# Owner(s): ["oncall: distributed"]

import copy
import functools
import itertools
import sys
import unittest
from typing import Any, Callable, Dict, List, Optional, Tuple, Type, Union

import torch
import torch.nn as nn
from torch import distributed as dist
from torch.distributed.fsdp import (
    BackwardPrefetch,
    CPUOffload,
    FullyShardedDataParallel as FSDP,
    MixedPrecision,
    ShardingStrategy,
)
from torch.distributed.fsdp._common_utils import clean_tensor_name
from torch.distributed.fsdp.wrap import always_wrap_policy, ModuleWrapPolicy
from torch.nn import TransformerDecoderLayer, TransformerEncoderLayer
from torch.nn.parallel.distributed import DistributedDataParallel as DDP
from torch.testing._internal.common_cuda import TEST_CUDA
from torch.testing._internal.common_distributed import skip_if_lt_x_gpu
from torch.testing._internal.common_fsdp import (
    CUDAInitMode,
    FSDPInitMode,
    FSDPTest,
    NestedWrappedModule,
    TransformerWithSharedParams,
)
from torch.testing._internal.common_utils import (
    instantiate_parametrized_tests,
    parametrize,
    run_tests,
    TEST_WITH_DEV_DBG_ASAN,
    TestCase,
)

if not dist.is_available():
    print("Distributed not available, skipping tests", file=sys.stderr)
    sys.exit(0)

if TEST_WITH_DEV_DBG_ASAN:
    print(
        "Skip dev-asan as torch + multiprocessing spawn have known issues",
        file=sys.stderr,
    )
    sys.exit(0)


class TestFSDPUseOrigParamsMultipleParamGroups(FSDPTest):
    """Tests multiple parameter groups."""

    @property
    def world_size(self) -> int:
        return 2

    def _get_param_groups(self, model: nn.Module) -> List[Dict[str, Any]]:
        """
        Constructs separate parameter groups for weights, biases, and other
        parameters.
        """
        param_groups = [
            {"params": [], "weight_decay": 0.1, "lr": 1e-2},
            {"params": [], "weight_decay": 0.01, "lr": 1e-3},
            {"params": []},
        ]
        for param_name, param in model.named_parameters():
            if "weight" in param_name:
                param_groups[0]["params"].append(param)
            elif "bias" in param_name:
                param_groups[1]["params"].append(param)
            else:
                param_groups[2]["params"].append(param)
        return param_groups

    def _get_optim(
        self,
        model: nn.Module,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
    ) -> torch.optim.Optimizer:
        """
        Constructs an Adam optimizer with three parameter groups, one for
        weights, one for biases, and one for everything else, each with
        different weight decay and learning rates.
        """
        param_groups = self._get_param_groups(model)
        return optim_class(param_groups, lr=5e-3, foreach=multi_tensor)

    def _get_ddp_transformer(self, find_unused_params: bool) -> DDP:
        """Returns a transformer with shared parameters wrapped with DDP."""
        model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            CUDAInitMode.CUDA_BEFORE,
            deterministic=True,
        )
        ddp_model = DDP(
            model,
            device_ids=[self.rank],
            find_unused_parameters=find_unused_params,
        )
        return ddp_model

    def _get_fsdp_transformer_and_optim(
        self,
        cuda_init_mode: CUDAInitMode,
        init_optim_before_wrap: bool,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
        sharding_strategy: ShardingStrategy,
        backward_prefetch: Optional[BackwardPrefetch],
        cpu_offload: CPUOffload,
    ) -> Tuple[FSDP, torch.optim.Optimizer]:
        """
        Returns a transformer with shared parameters wrapped with FSDP and a
        corresponding optimizer.
        """
        # Each transformer layer has multiple linear layers, so this policy, in
        # combination with the parameter group construction, ensures different
        # hyperparameter settings within one `FlatParameter`
        fsdp_kwargs = {
            "auto_wrap_policy": ModuleWrapPolicy(
                {
                    TransformerEncoderLayer,
                    TransformerDecoderLayer,
                }
            ),
            "use_orig_params": True,
            "sharding_strategy": sharding_strategy,
            "backward_prefetch": backward_prefetch,
            "cpu_offload": cpu_offload,
        }
        model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            cuda_init_mode,
            deterministic=True,
        )
        if init_optim_before_wrap:
            fsdp_optim = self._get_optim(model, optim_class, multi_tensor)
            fsdp_model = FSDP(model, self.process_group, **fsdp_kwargs)
        else:
            fsdp_model = FSDP(model, self.process_group, **fsdp_kwargs)
            fsdp_optim = self._get_optim(fsdp_model, optim_class, multi_tensor)
        if (
            cuda_init_mode == CUDAInitMode.CUDA_AFTER
            and not fsdp_model.cpu_offload.offload_params
        ):
            fsdp_model = fsdp_model.cuda()
        return fsdp_model, fsdp_optim

    def _check_train_parity(
        self,
        ddp_model: DDP,
        ddp_optim: torch.optim.Optimizer,
        fsdp_model: FSDP,
        fsdp_optim: torch.optim.Optimizer,
        set_to_none: bool,
        num_iters: int = 10,
    ):
        """Checks training parity between DDP and FSDP."""
        device = torch.device("cuda")
        for i in range(num_iters):
            iter_losses = []
            for model, optim in ((ddp_model, ddp_optim), (fsdp_model, fsdp_optim)):
                module = model.module
                # Test two different `zero_grad()` timings
                if i % 2 == 0:
                    optim.zero_grad(set_to_none=set_to_none)  # pre-forward
                inp = module.get_input(device)
                output = model(*inp)
                loss = module.get_loss(inp, output).to(device)
                iter_losses.append(loss)
                if i % 2 == 1:
                    optim.zero_grad(set_to_none=set_to_none)  # pre-backward
                module.run_backward(loss)
                # Perform the DDP optimizer step on CPU to match FSDP if needed
                if model is ddp_model and fsdp_model.cpu_offload.offload_params:
                    model.to(torch.device("cpu"))
                optim.step()
                if model is ddp_model and fsdp_model.cpu_offload.offload_params:
                    model.to(device)
            torch.testing.assert_close(iter_losses[0], iter_losses[1])
            iter_losses.clear()
        self._check_ddp_fsdp_param_parity(ddp_model, fsdp_model)

    def _check_ddp_fsdp_param_parity(self, ddp_model: DDP, fsdp_model: FSDP):
        with FSDP.summon_full_params(fsdp_model):
            for (n1, p1), (n2, p2) in zip(
                ddp_model.module.named_parameters(), fsdp_model.named_parameters()
            ):
                # Allow for FSDP prefixes
                self.assertEqual(n1, clean_tensor_name(n2))
                torch.testing.assert_close(p1, p2)

    def _get_sharding_strategy_from_str(
        self, sharding_strategy_str: str
    ) -> ShardingStrategy:
        if sharding_strategy_str == "no_shard":
            sharding_strategy = ShardingStrategy.NO_SHARD
        elif sharding_strategy_str == "shard_grad_op":
            sharding_strategy = ShardingStrategy.SHARD_GRAD_OP
        elif sharding_strategy_str == "full_shard":
            sharding_strategy = ShardingStrategy.FULL_SHARD
        else:
            raise ValueError(f"Invalid string: {sharding_strategy_str}")
        return sharding_strategy

    @skip_if_lt_x_gpu(2)
    @parametrize(
        "sharding_strategy_str",
        ["no_shard", "shard_grad_op", "full_shard"],
    )
    def test_diff_hyperparams(self, sharding_strategy_str: str):
        """
        Tests FSDP parity with DDP when using multiple parameter groups with
        different hyperparameter settings.
        """
        sharding_strategy = self._get_sharding_strategy_from_str(sharding_strategy_str)
        self.run_subtests(
            {
                "cuda_init_mode": [
                    CUDAInitMode.CUDA_BEFORE,
                    CUDAInitMode.CUDA_AFTER,
                ],
                "init_optim_before_wrap": [False, True],
                "optim_class": [torch.optim.AdamW],
                "multi_tensor": [False, True],
                "set_to_none": [False, True],
                "backward_prefetch": [
                    None,
                    BackwardPrefetch.BACKWARD_PRE,
                    BackwardPrefetch.BACKWARD_POST,
                ],
            },
            self._test_diff_hyperparams,
            cpu_offload=CPUOffload(offload_params=False),
            sharding_strategy=sharding_strategy,
        )

    @skip_if_lt_x_gpu(2)
    @parametrize(
        "sharding_strategy_str",
        ["no_shard", "shard_grad_op", "full_shard"],
    )
    def test_diff_hyperparams_cpu_offload(self, sharding_strategy_str: str):
        """
        Tests FSDP parity with DDP when using multiple parameter groups with
        different hyperparameter settings with CPU offloading enabled. This is
        separate from :meth:`test_diff_hyperparams` because CPU offloading has
        some issues with subtesting for some specific subtesting configs (e.g.,
        with ``offload_params=False`` followed by ``True`` but not vice versa).
        """
        sharding_strategy = self._get_sharding_strategy_from_str(sharding_strategy_str)
        self._test_diff_hyperparams(
            cuda_init_mode=CUDAInitMode.CUDA_BEFORE,
            init_optim_before_wrap=False,
            optim_class=torch.optim.Adam,
            multi_tensor=False,
            set_to_none=False,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            cpu_offload=CPUOffload(offload_params=True),
            sharding_strategy=sharding_strategy,
        )

    def _test_diff_hyperparams(
        self,
        cuda_init_mode: CUDAInitMode,
        init_optim_before_wrap: bool,
        optim_class: Type[torch.optim.Optimizer],
        multi_tensor: bool,
        set_to_none: bool,
        backward_prefetch: Optional[BackwardPrefetch],
        cpu_offload: CPUOffload,
        sharding_strategy: ShardingStrategy,
    ):
        """
        Args:
            init_optim_before_wrap (bool): If ``True``, initializes the
                FSDP optimizer before wrapping the model with FSDP; otherwise,
                initializes the FSDP optimizer after wrapping the model with
                FSDP. We permit both forms of initialization to give users
                flexibility.
        """
        if cuda_init_mode == CUDAInitMode.CUDA_AFTER and cpu_offload.offload_params:
            return  # not supported
        ddp_model = self._get_ddp_transformer(find_unused_params=False)
        ddp_optim = self._get_optim(ddp_model, optim_class, multi_tensor)
        fsdp_model, fsdp_optim = self._get_fsdp_transformer_and_optim(
            cuda_init_mode=cuda_init_mode,
            init_optim_before_wrap=init_optim_before_wrap,
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            sharding_strategy=sharding_strategy,
            backward_prefetch=backward_prefetch,
            cpu_offload=cpu_offload,
        )
        self._check_train_parity(
            ddp_model, ddp_optim, fsdp_model, fsdp_optim, set_to_none
        )

    @skip_if_lt_x_gpu(2)
    def test_diff_trainability(self):
        """
        Tests FSDP parity with DDP when using multiple parameter groups and
        freezing the parameters in one parameter group.
        """
        self.run_subtests(
            {
                "multi_tensor": [False, True],
                "sharding_strategy": [
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                    ShardingStrategy.NO_SHARD,
                ],
            },
            self._test_diff_trainability,
        )

    def _test_diff_trainability(
        self,
        multi_tensor: bool,
        sharding_strategy: ShardingStrategy,
    ):
        optim_class = torch.optim.Adam
        ddp_model = self._get_ddp_transformer(find_unused_params=True)
        ddp_optim = self._get_optim(ddp_model, optim_class, multi_tensor)
        fsdp_model, fsdp_optim = self._get_fsdp_transformer_and_optim(
            cuda_init_mode=CUDAInitMode.CUDA_BEFORE,
            init_optim_before_wrap=False,
            optim_class=optim_class,
            multi_tensor=multi_tensor,
            sharding_strategy=sharding_strategy,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            cpu_offload=None,
        )
        # Freeze all biases (which happen to be in the same parameter group)
        for param_name, param in ddp_model.named_parameters():
            if "bias" in param_name:
                param.requires_grad_(False)
        for param_name, param in fsdp_model.named_parameters():
            if "bias" in param_name:
                param.requires_grad_(False)
        self._check_train_parity(ddp_model, ddp_optim, fsdp_model, fsdp_optim, False)

    @skip_if_lt_x_gpu(2)
    def test_multiple_optimizers(self):
        """
        Tests using two optimizers where only one sets gradients to ``None``.
        """
        self.run_subtests(
            {
                "sharding_strategy": [
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                ]
            },
            self._test_multiple_optimizers,
        )

    def _test_multiple_optimizers(self, sharding_strategy: ShardingStrategy):
        ddp_model = self._get_ddp_transformer(find_unused_params=True)
        ddp_param_groups = self._get_param_groups(ddp_model)
        assert len(ddp_param_groups) == 3, f"{len(ddp_param_groups)}"
        (
            fsdp_model,
            _,
        ) = self._get_fsdp_transformer_and_optim(  # ignore returned optimizer
            cuda_init_mode=CUDAInitMode.CUDA_BEFORE,
            init_optim_before_wrap=False,
            optim_class=torch.optim.Adam,  # ignored
            multi_tensor=False,  # ignored
            sharding_strategy=sharding_strategy,
            backward_prefetch=BackwardPrefetch.BACKWARD_PRE,
            cpu_offload=None,
        )
        fsdp_param_groups = self._get_param_groups(fsdp_model)
        assert len(fsdp_param_groups) == 3, f"{len(fsdp_param_groups)}"
        ddp_optims = []
        fsdp_optims = []
        # For the transformer model, every parameter is either a weight or a
        # bias, so we only use the first two parameter groups. Moreover, we use
        # Adam and AdamW in particular since they both use bias correction
        # dependent on the step, which is incremented even if a parameter has a
        # zero gradient but not if the gradient is `None`. This is to test that
        # we are differentiating between a zero and `None` gradient correctly.
        optim_ctors = [
            functools.partial(torch.optim.Adam, lr=5e-3),
            functools.partial(torch.optim.AdamW, lr=1e-2),
        ]

        for optim_ctor, ddp_param_group, fsdp_param_group in zip(
            optim_ctors,
            ddp_param_groups[:2],
            fsdp_param_groups[:2],
        ):
            ddp_optims.append(optim_ctor(ddp_param_group["params"]))
            fsdp_optims.append(optim_ctor(fsdp_param_group["params"]))
        device = torch.device("cuda")

        # Check that there exists a `FlatParameter` that has both a weight and
        # a bias in this rank's shard
        has_both = False
        for fsdp_module in FSDP.fsdp_modules(fsdp_model):
            for handle in fsdp_module._handles:
                flat_param = handle.flat_param
                assert flat_param._params is not None
                has_weight = False
                has_bias = False
                for param, fqn in zip(flat_param._params, flat_param._fqns):
                    if "weight" in fqn and param.numel() > 0:
                        has_weight = True
                    elif "bias" in fqn and param.numel() > 0:
                        has_bias = True
                has_both |= has_weight and has_bias
        assert has_both, (
            f"Rank {self.rank} does not have a `FlatParameter` with both a "
            "weight and a bias in its shard, meaning that this test is vacuous"
        )

        # Run one iteration to generate gradients
        def run_iter():
            iter_losses = []
            for model, optims in ((ddp_model, ddp_optims), (fsdp_model, fsdp_optims)):
                module = model.module
                inp = module.get_input(device)
                output = model(*inp)
                loss = module.get_loss(inp, output).to(device)
                iter_losses.append(loss)
                module.run_backward(loss)
                for optim in optims:
                    optim.step()
            torch.testing.assert_close(iter_losses[0], iter_losses[1])
            iter_losses.clear()
            self._check_ddp_fsdp_param_parity(ddp_model, fsdp_model)

        run_iter()

        # Only set the weights' gradients to None
        ddp_optims[0].zero_grad(set_to_none=True)
        fsdp_optims[0].zero_grad(set_to_none=True)
        inp = ddp_model.module.get_input(device)
        ddp_output = ddp_model(*inp)
        fsdp_output = fsdp_model(*inp)

        # Check that FSDP correctly exposes gradients even after forward
        # (namely, `None` for weights and non-`None` for biases)
        for (ddp_n, ddp_p), (fsdp_n, fsdp_p) in zip(
            ddp_model.module.named_parameters(),
            fsdp_model.named_parameters(),
        ):
            self.assertEqual(ddp_n, clean_tensor_name(fsdp_n))
            if fsdp_p.numel() == 0:
                # Not in this rank's shard
                self.assertTrue(fsdp_p.grad is None)
                continue
            if ddp_p.grad is None:
                self.assertTrue(fsdp_p.grad is None)
            else:
                self.assertEqual(ddp_p.flatten(), fsdp_p.flatten())
                self.assertEqual(ddp_p.grad.flatten(), fsdp_p.grad.flatten())
        self._check_ddp_fsdp_param_parity(ddp_model, fsdp_model)

        # Finish the iteration (backward pass and optimizer step)
        ddp_loss = ddp_model.module.get_loss(inp, ddp_output).to(device)
        fsdp_loss = fsdp_model.module.get_loss(inp, fsdp_output).to(device)
        ddp_model.module.run_backward(ddp_loss)
        fsdp_model.module.run_backward(fsdp_loss)
        for optim in itertools.chain(ddp_optims, fsdp_optims):
            optim.step()
        self._check_ddp_fsdp_param_parity(ddp_model, fsdp_model)

        # Run one more iteration to confirm bias corrections are correct
        run_iter()
        self._check_ddp_fsdp_param_parity(ddp_model, fsdp_model)


class TestFSDPUseOrigParamsUnshardReshard(FSDPTest):
    """Tests the unshard/reshard flow."""

    @property
    def world_size(self) -> int:
        return 2

    def _get_models_and_optims(
        self,
        local_model: nn.Module,
        use_ddp_as_ref: bool,
        **fsdp_kwargs: Dict[str, Any],
    ):
        LR = 5e-2
        if use_ddp_as_ref:
            ref_model = DDP(copy.deepcopy(local_model), device_ids=[self.rank])
        else:
            ref_model = FSDP(
                copy.deepcopy(local_model), use_orig_params=False, **fsdp_kwargs
            )
        ref_optim = torch.optim.Adam(ref_model.parameters(), foreach=False, lr=LR)
        fsdp_model = FSDP(
            copy.deepcopy(local_model),
            use_orig_params=True,
            **fsdp_kwargs,
        )
        fsdp_optim = torch.optim.Adam(fsdp_model.parameters(), foreach=False, lr=LR)
        return ref_model, ref_optim, fsdp_model, fsdp_optim

    def _get_transformer_with_shared_params_and_policy(
        self,
    ) -> Tuple[nn.Module, Callable]:
        # Disable batch norm since DDP errors from broadcasting BN buffers
        # (which is considered in-place) when running multiple forwards
        add_bn = False
        model = TransformerWithSharedParams.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            CUDAInitMode.CUDA_BEFORE,
            fsdp_kwargs={},
            deterministic=True,
            add_bn=add_bn,
        )
        auto_wrap_policy = ModuleWrapPolicy(
            {TransformerEncoderLayer, TransformerDecoderLayer}
        )
        return model, auto_wrap_policy

    def _get_nested_wrapped_module_and_policy(self) -> Tuple[nn.Module, Callable]:
        model = NestedWrappedModule.init(
            self.process_group,
            FSDPInitMode.NO_FSDP,
            CUDAInitMode.CUDA_BEFORE,
            fsdp_kwargs={},
            deterministic=True,
        )
        auto_wrap_policy = ModuleWrapPolicy({nn.Sequential})
        return model, auto_wrap_policy

    def _check_parameter_parity(
        self, ref_model: Union[DDP, FSDP], fsdp_model: FSDP
    ) -> None:
        if isinstance(ref_model, DDP):
            with FSDP.summon_full_params(fsdp_model):
                for (n1, p1), (n2, p2) in zip(
                    ref_model.module.named_parameters(),
                    fsdp_model.named_parameters(),
                ):
                    self.assertEqual(n1, n2)
                    torch.testing.assert_close(p1, p2)
        elif isinstance(ref_model, FSDP):
            with FSDP.summon_full_params(ref_model), FSDP.summon_full_params(
                fsdp_model
            ):
                for (n1, p1), (n2, p2) in zip(
                    ref_model.named_parameters(),
                    fsdp_model.named_parameters(),
                ):
                    self.assertEqual(n1, n2)
                    torch.testing.assert_close(p1, p2)
        else:
            raise ValueError(f"Unknown reference model type: {type(ref_model)}")

    def _get_fsdp_parity_subtest_config(self):
        return {
            "sharding_strategy": [
                ShardingStrategy.NO_SHARD,
                ShardingStrategy.SHARD_GRAD_OP,
                ShardingStrategy.FULL_SHARD,
            ],
        }

    @skip_if_lt_x_gpu(2)
    @parametrize("offload_params", [False, True])
    def test_multiple_forward(self, offload_params: bool):
        """
        Tests that ``use_orig_params=True`` has parity with DDP or FSDP 
        ``use_orig_params=False`` when running multiple forward passes before a
        backward pass.

        NOTE: We must compare with FSDP ``use_orig_params=False`` as reference
        when CPU offloading since CPU kernels give slightly different results,
        and DDP does not natively support CPU offloading.
        """
        cpu_offload = CPUOffload(offload_params=offload_params)
        self.run_subtests(
            self._get_fsdp_parity_subtest_config(),
            self._test_multiple_forward,
            cpu_offload=cpu_offload,
        )

    def _test_multiple_forward(
        self, sharding_strategy: ShardingStrategy, cpu_offload: CPUOffload
    ):
        # Run with two different models for greater confidence
        (
            model,
            auto_wrap_policy,
        ) = self._get_transformer_with_shared_params_and_policy()
        self._test_multiple_forward_base(
            model, auto_wrap_policy, sharding_strategy, cpu_offload
        )
        model, auto_wrap_policy = self._get_nested_wrapped_module_and_policy()
        self._test_multiple_forward_base(
            model, auto_wrap_policy, sharding_strategy, cpu_offload
        )

    def _test_multiple_forward_base(
        self,
        model: nn.Module,
        auto_wrap_policy: Callable,
        sharding_strategy: ShardingStrategy,
        cpu_offload: CPUOffload,
    ):
        ref_model, ref_optim, fsdp_model, fsdp_optim = self._get_models_and_optims(
            model,
            use_ddp_as_ref=not cpu_offload.offload_params,
            auto_wrap_policy=auto_wrap_policy,
            sharding_strategy=sharding_strategy,
            cpu_offload=cpu_offload,
        )
        device = torch.device("cuda")
        torch.manual_seed(self.rank + 1)
        for i in range(10):
            inp1 = fsdp_model.get_input(device)
            _inp2 = fsdp_model.get_input(device)
            inp2 = tuple(t + torch.ones_like(t) for t in _inp2)
            losses1 = []
            losses2 = []
            losses = []
            for _model, _optim in ((ref_model, ref_optim), (fsdp_model, fsdp_optim)):
                _optim.zero_grad()
                loss1 = _model(*inp1)
                losses1.append(loss1)
                loss2 = _model(*inp2)
                losses2.append(loss2)
                loss = (loss1 + loss2).sum()
                losses.append(loss)
                loss.backward()
                _optim.step()
            self.assertEqual(losses1[0], losses1[1])
            self.assertEqual(losses2[0], losses2[1])
            self.assertEqual(losses[0], losses[1])
        self._check_parameter_parity(ref_model, fsdp_model)

    @skip_if_lt_x_gpu(2)
    @parametrize("offload_params", [False, True])
    def test_summon_between_two_forwards(self, offload_params: bool):
        """
        Tests that ``use_orig_params=True`` has parity with DDP or FSDP
        ``use_orig_params=False`` when running a forward pass,
        :meth:`summon_full_params`, and another forward pass before a backward
        pass.

        NOTE: We must compare with FSDP ``use_orig_params=False`` as reference
        when CPU offloading since CPU kernels give slightly different results,
        and DDP does not natively support CPU offloading.
        """
        cpu_offload = CPUOffload(offload_params=offload_params)
        self.run_subtests(
            self._get_fsdp_parity_subtest_config(),
            self._test_summon_between_two_forwards,
            cpu_offload=cpu_offload,
        )

    def _test_summon_between_two_forwards(
        self, sharding_strategy: ShardingStrategy, cpu_offload: CPUOffload
    ):
        # Run with two different models for greater confidence
        (
            model,
            auto_wrap_policy,
        ) = self._get_transformer_with_shared_params_and_policy()
        self._test_summon_between_two_forwards_base(
            model, auto_wrap_policy, sharding_strategy, cpu_offload
        )
        model, auto_wrap_policy = self._get_nested_wrapped_module_and_policy()
        self._test_summon_between_two_forwards_base(
            model, auto_wrap_policy, sharding_strategy, cpu_offload
        )

    def _test_summon_between_two_forwards_base(
        self,
        model: nn.Module,
        auto_wrap_policy: Callable,
        sharding_strategy: ShardingStrategy,
        cpu_offload: CPUOffload,
    ):
        ref_model, ref_optim, fsdp_model, fsdp_optim = self._get_models_and_optims(
            model,
            use_ddp_as_ref=not cpu_offload.offload_params,
            auto_wrap_policy=auto_wrap_policy,
            sharding_strategy=sharding_strategy,
            cpu_offload=cpu_offload,
        )
        device = torch.device("cuda")
        for _ in range(10):
            ref_optim.zero_grad()
            fsdp_optim.zero_grad()

            inp1 = fsdp_model.get_input(device)
            ddp_loss1 = ref_model(*inp1)
            fsdp_loss1 = fsdp_model(*inp1)
            self.assertEqual(ddp_loss1, fsdp_loss1)

            # Calls into `summon_full_params()`
            self._check_parameter_parity(ref_model, fsdp_model)

            inp2 = fsdp_model.get_input(device)
            ddp_loss2 = ref_model(*inp2)
            fsdp_loss2 = fsdp_model(*inp2)
            self.assertEqual(ddp_loss2, fsdp_loss2)

            ddp_loss = (ddp_loss1 + ddp_loss2).sum()
            fsdp_loss = (fsdp_loss1 + fsdp_loss2).sum()
            ddp_loss.backward()
            fsdp_loss.backward()
            ref_optim.step()
            fsdp_optim.step()
        self._check_parameter_parity(ref_model, fsdp_model)


class TestFSDPUseOrigParamsParamAccess(FSDPTest):
    """Tests original parameter access."""

    @property
    def world_size(self):
        # Force a world size of 2 since the tests hard code to the FSDP
        # sharding strategy to check sharded parameter parity
        return 2

    @skip_if_lt_x_gpu(2)
    def test_access_params_after_forward(self):
        """
        Tests that accessing the original parameters after the forward but
        before the backward. Notably, this is not supported when
        ``use_orig_params=False``. However, for ``True``, FSDP exposes the
        (flattened) sharded original parameters, making it possible.
        """
        self.run_subtests(
            {
                "sharding_strategy": [
                    ShardingStrategy.NO_SHARD,
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                ],
            },
            self._test_access_params_after_forward,
        )

    def _test_access_params_after_forward(
        self,
        sharding_strategy: ShardingStrategy,
    ):
        # NOTE: This test needs to be changed if the FSDP sharding algorithm
        # changes. It is still valuable until such a change to sanity check the
        # `use_orig_params=True` implementation.
        class Model(nn.Module):
            def __init__(self):
                super().__init__()
                torch.manual_seed(42)
                # 5 * 5 = 25 numel -> pad to 26 -> 13 on each rank
                self.lin1 = nn.Linear(5, 5, bias=False)
                # 5 * 7 + 7 = 42 numel -> no pad -> 21 on each rank
                # 21 of weight on rank 0; 14 of weight and 7 of bias on rank 1
                self.lin2 = nn.Linear(5, 7)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                z = self.lin1(x)
                z = nn.functional.relu(z)
                z = self.lin2(z)
                return z

            def get_input(self, device: torch.device) -> Tuple[torch.Tensor, ...]:
                return (torch.randn((2, 5)).to(device),)

            def get_loss(self, inp, out):
                return out.sum()

        def check_parameter_parity(ddp_model, fsdp_model):
            assert self.rank in (
                0,
                1,
            ), f"Expects world size of 2 but got {self.world_size}"
            for (n1, p1), (n2, p2) in zip(
                ddp_model.module.named_parameters(),
                fsdp_model.named_parameters(),
            ):
                self.assertEqual(n1, clean_tensor_name(n2))
                if sharding_strategy == ShardingStrategy.NO_SHARD:
                    # For `NO_SHARD`, do nothing since the original parameters
                    # are unflattened
                    pass
                # Otherwise, case on the parameter (see the model definition)
                elif n1 == "lin1.weight":
                    if self.rank == 0:
                        p1 = p1.flatten()[:13]
                    elif self.rank == 1:
                        p1 = p1.flatten()[13:]
                elif n1 == "lin2.weight":
                    if self.rank == 0:
                        p1 = p1.flatten()[:21]
                    elif self.rank == 1:
                        p1 = p1.flatten()[21:]
                elif n1 == "lin2.bias":
                    if self.rank == 0:
                        p1 = torch.empty(0, device=p1.device)
                    elif self.rank == 1:
                        p1 = p1.flatten()
                torch.testing.assert_close(p1, p2)

        ddp_model = DDP(Model().cuda(), device_ids=[self.rank])
        fsdp_model = FSDP(
            Model().cuda(),
            sharding_strategy=sharding_strategy,
            auto_wrap_policy=always_wrap_policy,
            use_orig_params=True,
        )
        LR = 1e-2
        ddp_optim = torch.optim.Adam(ddp_model.parameters(), lr=LR)
        fsdp_optim = torch.optim.Adam(fsdp_model.parameters(), lr=LR)
        device = torch.device("cuda")

        inp = fsdp_model.get_input(device)
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        check_parameter_parity(ddp_model, fsdp_model)

        ddp_loss = ddp_model.module.get_loss(inp, ddp_out)
        fsdp_loss = fsdp_model.get_loss(inp, fsdp_out)
        ddp_loss.backward()
        fsdp_loss.backward()
        ddp_optim.step()
        fsdp_optim.step()
        check_parameter_parity(ddp_model, fsdp_model)

        inp = fsdp_model.get_input(device)
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        check_parameter_parity(ddp_model, fsdp_model)


class TestFSDPUseOrigParamsWriteback(FSDPTest):
    """Tests parameter and gradient writeback."""

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            torch.manual_seed(42)
            self.lin1 = nn.Linear(5, 5, bias=True)
            self.lin2 = nn.Linear(5, 7, bias=True)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            z = self.lin1(x)
            z = nn.functional.relu(z)
            z = self.lin2(z)
            return z

        def get_input(self, device: torch.device) -> Tuple[torch.Tensor, ...]:
            return (torch.randn((2, 5)).to(device),)

        def get_loss(self, inp, out):
            return out.sum()

    @property
    def world_size(self):
        # Force a world size of 2 since the tests hard code to the FSDP
        # sharding strategy
        return 2

    def _check_param_parity(self, ddp_model: DDP, fsdp_model: FSDP):
        with FSDP.summon_full_params(fsdp_model):
            for (n1, p1), (n2, p2) in zip(
                ddp_model.module.named_parameters(),
                fsdp_model.named_parameters(),
            ):
                self.assertEqual(n1, n2)
                torch.testing.assert_close(p1, p2)

    @skip_if_lt_x_gpu(2)
    def test_param_writeback(self):
        """Tests that changes to the original parameters are written back."""
        self.run_subtests(
            {
                "change_first_weight": [True, False],  # first vs. second `weight`
                "change_data": [True, False],  # change `.data` vs. variable itself
            },
            self._test_param_writeback,
        )

    def _test_param_writeback(self, change_first_weight: bool, change_data: bool):
        def transform_param(param: nn.Parameter) -> nn.Parameter:
            return nn.Parameter(torch.ones_like(param) * 2)

        # Check that the writeback propagates
        ddp_model = DDP(
            TestFSDPUseOrigParamsWriteback.Model().cuda(), device_ids=[self.rank]
        )
        fsdp_model = FSDP(
            TestFSDPUseOrigParamsWriteback.Model().cuda(), use_orig_params=True
        )
        ddp = ddp_model.module  # for brevity
        fsdp = fsdp_model.module
        if change_first_weight:
            if change_data:
                ddp.lin1.weight.data = transform_param(ddp.lin1.weight)
                fsdp.lin1.weight.data = transform_param(fsdp.lin1.weight)
            else:
                ddp.lin1.weight = transform_param(ddp.lin1.weight)
                fsdp.lin1.weight = transform_param(fsdp.lin1.weight)
        else:
            if change_data:
                ddp.lin2.weight.data = transform_param(ddp.lin2.weight)
                fsdp.lin2.weight.data = transform_param(fsdp.lin2.weight)
            else:
                ddp.lin2.weight = transform_param(ddp.lin2.weight)
                fsdp.lin2.weight = transform_param(fsdp.lin2.weight)
        self._check_param_parity(ddp_model, fsdp_model)  # triggers a writeback

    @skip_if_lt_x_gpu(2)
    def test_grad_writeback(self):
        """
        Tests that changes to the original parameters' gradients are written
        back.
        """
        self.run_subtests(
            {
                "change_first_weight_grad": [False, True],
                "change_data": [False, True],  # change `.data` vs. variable itself
                "set_to_none": [False, True],
            },
            self._test_grad_writeback,
        )

    def _test_grad_writeback(
        self,
        change_first_weight_grad: bool,
        change_data: bool,
        set_to_none: bool,
    ):
        if change_data and set_to_none:
            return  # not well-defined

        def transform_grad(param: nn.Parameter) -> nn.Parameter:
            return None if set_to_none else torch.ones_like(param) * 2

        ddp_model = DDP(
            TestFSDPUseOrigParamsWriteback.Model().cuda(), device_ids=[self.rank]
        )
        fsdp_model = FSDP(
            TestFSDPUseOrigParamsWriteback.Model().cuda(), use_orig_params=True
        )
        LR = 1e-2
        # TODO: If we add `summon_full_params(with_grads=True)`, then replace
        # the following. For now, we use the optimizer step as a surrogate for
        # checking that gradients were written back.
        ddp_optim = torch.optim.Adam(ddp_model.parameters(), lr=LR)
        fsdp_optim = torch.optim.Adam(fsdp_model.parameters(), lr=LR)

        # Generate an initial gradient
        inp = fsdp_model.get_input(torch.device("cuda"))
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        ddp_out.sum().backward()
        fsdp_out.sum().backward()

        # Change the gradient through the original parameters
        ddp = ddp_model.module  # for brevity
        fsdp = fsdp_model.module
        if change_first_weight_grad:
            if change_data:
                ddp.lin1.weight.grad.data = transform_grad(ddp.lin1.weight)
                if fsdp.lin1.weight.grad is not None:
                    fsdp.lin1.weight.grad.data = transform_grad(fsdp.lin1.weight)
            else:
                ddp.lin1.weight.grad = transform_grad(ddp.lin1.weight)
                fsdp.lin1.weight.grad = transform_grad(fsdp.lin1.weight)
        else:
            if change_data:
                ddp.lin2.weight.grad.data = transform_grad(ddp.lin2.weight)
                if fsdp.lin2.weight.grad is not None:
                    fsdp.lin2.weight.grad.data = transform_grad(fsdp.lin2.weight)
            else:
                ddp.lin2.weight.grad = transform_grad(ddp.lin2.weight)
                fsdp.lin2.weight.grad = transform_grad(fsdp.lin2.weight)
        ddp_optim.step()
        fsdp_optim.step()
        self._check_param_parity(ddp_model, fsdp_model)  # triggers a writeback

        # Intentionally do not zero the gradient to check writeback
        inp = fsdp_model.get_input(torch.device("cuda"))
        ddp_out = ddp_model(*inp)
        fsdp_out = fsdp_model(*inp)
        ddp_out.sum().backward()
        fsdp_out.sum().backward()
        ddp_optim.step()
        fsdp_optim.step()
        self._check_param_parity(ddp_model, fsdp_model)  # triggers a writeback

    @skip_if_lt_x_gpu(2)
    def test_writeback_shape_mismatch(self):
        fsdp_model = FSDP(
            TestFSDPUseOrigParamsWriteback.Model().cuda(), use_orig_params=True
        )
        # Check that writing back with mismatched shape errors
        fsdp = fsdp_model.module  # for brevity
        assert self.rank in (0, 1), f"Expects world size of 2 but got {self.world_size}"
        with self.assertRaisesRegex(RuntimeError, "Cannot writeback"):
            # Change the gradient to a new one with 1 added to each dimension
            # to force a shape mismatch when writing back
            if self.rank == 0:
                # Change `lin1.weight.grad` since it exists on rank 0
                lin1_weight_shape = list(fsdp.lin1.weight.shape)
                for dim_index in range(len(lin1_weight_shape)):
                    lin1_weight_shape[dim_index] += 1
                fsdp.lin1.weight = nn.Parameter(
                    torch.randn(
                        torch.Size(lin1_weight_shape), device=fsdp.lin1.weight.device
                    )
                )
                fsdp.lin1.weight.grad = torch.randn(
                    torch.Size(lin1_weight_shape), device=fsdp.lin1.weight.device
                )
            elif self.rank == 1:
                # Change `lin2.weight.grad` since it exists (partially) on rank 1
                lin2_weight_shape = list(fsdp.lin2.weight.shape)
                for dim_index in range(len(lin2_weight_shape)):
                    lin2_weight_shape[dim_index] += 1
                fsdp.lin2.weight = nn.Parameter(
                    torch.randn(
                        torch.Size(lin2_weight_shape), device=fsdp.lin2.weight.device
                    )
                )
                fsdp.lin2.weight.grad = torch.randn(
                    torch.Size(lin2_weight_shape), device=fsdp.lin2.weight.device
                )
            with FSDP.summon_full_params(fsdp_model):  # triggers a writeback
                ...


class TestFSDPUseOrigParamsFQNs(FSDPTest):
    @skip_if_lt_x_gpu(2)
    def test_named_parameters_in_forward(self):
        """
        Tests that calling ``named_parameters()`` during forward returns FQNs
        and ``Tensor`` s corresponding to the original parameters.
        """
        param_shapes = [None, None]
        assert_equal_fn = self.assertEqual

        class Model(nn.Module):
            def __init__(self) -> None:
                super().__init__()
                self.lin = nn.Linear(5, 5)

            def forward(self, x: torch.Tensor) -> torch.Tensor:
                nonlocal param_shapes
                # Allow for FSDP prefixes
                param_names = [
                    clean_tensor_name(tup[0]) for tup in self.named_parameters()
                ]
                params = [tup[1] for tup in self.named_parameters()]
                assert (
                    param_shapes[0] is not None and param_shapes[1] is not None
                ), "`param_sizes` should be set"
                assert_equal_fn(
                    param_names,
                    [
                        "lin.weight",
                        "lin.bias",
                    ],
                )
                assert_equal_fn(params[0].shape, param_shapes[0])
                assert_equal_fn(params[1].shape, param_shapes[1])
                return self.lin(x)

        model = Model().cuda()
        # Save the *unsharded* original parameter shapes and check the shapes
        # match in the forward pass
        param_shapes[0] = model.lin.weight.shape
        param_shapes[1] = model.lin.bias.shape
        fsdp_model = FSDP(model, use_orig_params=True)
        inp = torch.randn((2, 5), device=torch.device("cuda"))
        fsdp_model(inp)


class TestFSDPUseOrigParamsNoSync(FSDPTest):
    @property
    def world_size(self) -> int:
        return 2

    @skip_if_lt_x_gpu(2)
    def test_no_sync_correctness(self):
        """
        Tests a basic ``no_sync()`` setup by comparing ``use_orig_params=True``
        against ``use_orig_params=False``.
        """
        self.run_subtests(
            {
                "sharding_strategy": [
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                    ShardingStrategy.NO_SHARD,
                ],
            },
            self._test_no_sync_correctness,
        )

    def _test_no_sync_correctness(self, sharding_strategy: ShardingStrategy):
        model = nn.Linear(3, 3, device="cuda")
        fsdp_kwargs = {
            "sharding_strategy": sharding_strategy,
        }
        model_use_flat_params = FSDP(
            copy.deepcopy(model), use_orig_params=False, **fsdp_kwargs
        )
        model_use_orig_params = FSDP(model, use_orig_params=True, **fsdp_kwargs)
        optim_use_flat_params = torch.optim.AdamW(
            model_use_flat_params.parameters(), foreach=True
        )
        optim_use_orig_params = torch.optim.AdamW(
            model_use_orig_params.parameters(), foreach=True
        )

        def _check_param_grad_parity(
            _baseline_model: nn.Module,
            _test_model: nn.Module,
        ):
            """
            This assumes that the model is ``nn.Linear(3, 3, bias=False)``
            (i.e. with a single weight parameter) to be able to directly
            compare the baseline and test models. On rank 1, the baseline
            includes 1 element of padding.
            """
            for flat_param, orig_param in zip(
                _baseline_model.parameters(), _test_model.parameters()
            ):
                # Baseline is permitted to have padding
                self.assertGreaterEqual(flat_param.numel(), orig_param.numel())
                unpadded_param_numel = orig_param.numel()
                # For `NO_SHARD`, `use_orig_params=True` presents unflattened
                # parameters, while `False` presents flattened ones
                torch.testing.assert_close(
                    flat_param[:unpadded_param_numel], orig_param.flatten()
                )
                # Gradient numel is different if right after `no_sync()` since
                # the gradient is unsharded, while the parameter is sharded
                unpadded_grad_numel = orig_param.grad.numel()
                # For `use_orig_params=False`, the unsharded gradient is
                # flattened, while for `True`, it is unflattened
                torch.testing.assert_close(
                    flat_param.grad[:unpadded_grad_numel].reshape(
                        orig_param.grad.shape
                    ),
                    orig_param.grad,
                )

        inp = torch.randn((2, 3), device="cuda")
        grad = torch.rand_like(inp)

        # Compute some reference gradients using one forward/backward
        out_use_flat_params = model_use_flat_params(inp)
        out_use_orig_params = model_use_orig_params(inp)
        torch.testing.assert_close(out_use_flat_params, out_use_orig_params)
        out_use_flat_params.backward(grad)
        out_use_orig_params.backward(grad)
        _check_param_grad_parity(model_use_flat_params, model_use_orig_params)
        ref_grads_use_flat_params = [
            param.grad.detach().clone() for param in model_use_flat_params.parameters()
        ]
        ref_grads_use_orig_params = [
            param.grad.detach().clone()
            for param in model_use_orig_params.parameters()
            if param.grad is not None
        ]

        # Run a forward/backward in `no_sync()`
        optim_use_flat_params.zero_grad(set_to_none=True)
        optim_use_orig_params.zero_grad(set_to_none=True)
        for model in (model_use_flat_params, model_use_orig_params):
            with model.no_sync():
                out = model(inp)
                out.backward(grad)
        _check_param_grad_parity(model_use_flat_params, model_use_orig_params)

        # Run a forward/backward outside `no_sync()`
        for model in (model_use_flat_params, model_use_orig_params):
            out = model(inp)
            out.backward(grad)
        _check_param_grad_parity(model_use_flat_params, model_use_orig_params)

        # Check that, since we accumulated gradients across 2 iterations, that
        # the new gradients are 2x the reference gradients
        grads_use_flat_params = [
            param.grad.detach().clone() for param in model_use_flat_params.parameters()
        ]
        grads_use_orig_params = [
            param.grad.detach().clone()
            for param in model_use_orig_params.parameters()
            if param.grad is not None
        ]
        for grad, ref_grad in zip(grads_use_flat_params, ref_grads_use_flat_params):
            torch.testing.assert_close(grad, 2 * ref_grad)
        for grad, ref_grad in zip(grads_use_orig_params, ref_grads_use_orig_params):
            torch.testing.assert_close(grad, 2 * ref_grad)

    @skip_if_lt_x_gpu(2)
    def test_no_sync_mixed_precision(self):
        """
        Tests that dtypes are as expected when using ``no_sync()`` with
        ``use_orig_params=True`` and parameter mixed precision.
        """
        self.run_subtests(
            {
                "sharding_strategy": [
                    ShardingStrategy.FULL_SHARD,
                    ShardingStrategy.SHARD_GRAD_OP,
                    ShardingStrategy.NO_SHARD,
                ]
            },
            self._test_no_sync_mixed_precision,
        )

    def _test_no_sync_mixed_precision(self, sharding_strategy: ShardingStrategy):
        model = nn.Linear(3, 3, device="cuda")
        mixed_precision = MixedPrecision(
            param_dtype=torch.float16,
            reduce_dtype=torch.float32,
        )
        fsdp_kwargs = {
            "sharding_strategy": sharding_strategy,
            "mixed_precision": mixed_precision,
            "use_orig_params": True,
        }
        fsdp_model = FSDP(model, **fsdp_kwargs)
        inp = torch.randn((2, 3), device="cuda")
        with fsdp_model.no_sync():
            # For each of these `no_sync()` backward passes, check that the
            # gradients are in the low precision parameter dtype (FP16)
            fsdp_model(inp).sum().backward()
            for param in fsdp_model.parameters():
                if param.grad is not None:
                    self.assertEqual(param.grad.dtype, torch.float16)
            fsdp_model(inp).sum().backward()
            for param in fsdp_model.parameters():
                if param.grad is not None:
                    self.assertEqual(param.grad.dtype, torch.float16)
        # For the backward pass outside `no_sync()`, check that the gradients
        # are cast to the full precision in preparation for the optimizer step
        fsdp_model(inp).sum().backward()
        for param in fsdp_model.parameters():
            if param.grad is not None:
                self.assertEqual(param.grad.dtype, torch.float32)


# Define this to be large enough to trigger stack corruption
NUM_SIZE0_TENSORS = 1000


class TestMultiTensorApply(TestCase):
    def test_multi_tensor_apply_size0_tensors_cpu(self):
        size0_tensors = [torch.empty(0, device="cpu") for _ in range(NUM_SIZE0_TENSORS)]
        # Check that this does not segfault
        torch._foreach_mul_(size0_tensors, 0.1)

    @unittest.skipIf(not TEST_CUDA, "no cuda")
    def test_multi_tensor_apply_size0_tensors_cuda(self):
        size0_tensors = [
            torch.empty(0, device="cuda") for _ in range(NUM_SIZE0_TENSORS)
        ]
        # Check that this does not segfault
        torch._foreach_mul_(size0_tensors, 0.1)


instantiate_parametrized_tests(TestFSDPUseOrigParamsMultipleParamGroups)
instantiate_parametrized_tests(TestFSDPUseOrigParamsUnshardReshard)
instantiate_parametrized_tests(TestFSDPUseOrigParamsParamAccess)
instantiate_parametrized_tests(TestFSDPUseOrigParamsFQNs)
instantiate_parametrized_tests(TestFSDPUseOrigParamsNoSync)

if __name__ == "__main__":
    run_tests()
