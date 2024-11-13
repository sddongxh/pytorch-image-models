""" Optimizer Factory w/ custom Weight Decay & Layer Decay support

Hacked together by / Copyright 2021 Ross Wightman
"""
import logging
from dataclasses import dataclass
from functools import partial
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, TypeVar, Union, Protocol, Iterator
from fnmatch import fnmatch
import importlib

import torch
import torch.nn as nn
import torch.optim as optim

from ._param_groups import param_groups_layer_decay, param_groups_weight_decay, group_parameters
from .adabelief import AdaBelief
from .adafactor import Adafactor
from .adafactor_bv import AdafactorBigVision
from .adamp import AdamP
from .adan import Adan
from .adopt import Adopt
from .lamb import Lamb
from .lars import Lars
from .lion import Lion
from .lookahead import Lookahead
from .madgrad import MADGRAD
from .nadam import Nadam
from .nadamw import NAdamW
from .nvnovograd import NvNovoGrad
from .radam import RAdam
from .rmsprop_tf import RMSpropTF
from .sgdp import SGDP
from .sgdw import SGDW

_logger = logging.getLogger(__name__)

# Type variables
T = TypeVar('T')
Params = Union[Iterator[nn.Parameter], Iterator[Dict[str, Any]]]
OptimType = TypeVar('OptimType', bound='optim.Optimizer')


def _import_class(class_string: str) -> Type:
    """Dynamically import a class from a string."""
    try:
        module_name, class_name = class_string.rsplit(".", 1)
        module = importlib.import_module(module_name)
        return getattr(module, class_name)
    except (ImportError, AttributeError) as e:
        raise ImportError(f"Could not import {class_string}: {e}")


class OptimizerCallable(Protocol):
    """Protocol for optimizer constructor signatures."""

    def __call__(self, params: Params, **kwargs) -> optim.Optimizer: ...


@dataclass(frozen=True)
class OptimInfo:
    """Immutable configuration for an optimizer.

    Attributes:
        name: Unique identifier for the optimizer
        opt_class: The optimizer class
        description: Brief description of the optimizer's characteristics and behavior
        has_eps: Whether the optimizer accepts epsilon parameter
        has_momentum: Whether the optimizer accepts momentum parameter
        has_betas: Whether the optimizer accepts a tuple of beta parameters
        num_betas: number of betas in tuple (valid IFF has_betas = True)
        defaults: Optional default parameters for the optimizer
    """
    name: str
    opt_class: Union[str, Type[optim.Optimizer]]
    description: str = ''
    has_eps: bool = True
    has_momentum: bool = False
    has_betas: bool = False
    num_betas: int = 2
    defaults: Optional[Dict[str, Any]] = None


class OptimizerRegistry:
    """Registry managing optimizer configurations and instantiation.

    This class provides a central registry for optimizer configurations and handles
    their instantiation with appropriate parameter groups and settings.
    """

    def __init__(self) -> None:
        self._optimizers: Dict[str, OptimInfo] = {}
        self._foreach_defaults: Set[str] = {'lion'}

    def register(self, info: OptimInfo) -> None:
        """Register an optimizer configuration.

        Args:
            info: The OptimInfo configuration containing name, type and description
        """
        name = info.name.lower()
        if name in self._optimizers:
            _logger.warning(f'Optimizer {name} already registered, overwriting')
        self._optimizers[name] = info

    def register_alias(self, alias: str, target: str) -> None:
        """Register an alias for an existing optimizer.

        Args:
            alias: The alias name
            target: The target optimizer name

        Raises:
            KeyError: If target optimizer doesn't exist
        """
        target = target.lower()
        if target not in self._optimizers:
            raise KeyError(f'Cannot create alias for non-existent optimizer {target}')
        self._optimizers[alias.lower()] = self._optimizers[target]

    def register_foreach_default(self, name: str) -> None:
        """Register an optimizer as defaulting to foreach=True."""
        self._foreach_defaults.add(name.lower())

    def list_optimizers(
            self,
            filter: str = '',
            exclude_filters: Optional[List[str]] = None,
            with_description: bool = False
    ) -> List[Union[str, Tuple[str, str]]]:
        """List available optimizer names, optionally filtered.

        Args:
            filter: Wildcard style filter string (e.g., 'adam*')
            exclude_filters: Optional list of wildcard patterns to exclude
            with_description: If True, return tuples of (name, description)

        Returns:
            List of either optimizer names or (name, description) tuples
        """
        names = sorted(self._optimizers.keys())

        if filter:
            names = [n for n in names if fnmatch(n, filter)]

        if exclude_filters:
            for exclude_filter in exclude_filters:
                names = [n for n in names if not fnmatch(n, exclude_filter)]

        if with_description:
            return [(name, self._optimizers[name].description) for name in names]
        return names

    def get_optimizer_info(self, name: str) -> OptimInfo:
        """Get the OptimInfo for an optimizer.

        Args:
            name: Name of the optimizer

        Returns:
            OptimInfo configuration

        Raises:
            ValueError: If optimizer is not found
        """
        name = name.lower()
        if name not in self._optimizers:
            raise ValueError(f'Optimizer {name} not found in registry')
        return self._optimizers[name]

    def get_optimizer_class(
            self,
            name_or_info: Union[str, OptimInfo],
            bind_defaults: bool = True,
    ) -> Union[Type[optim.Optimizer], OptimizerCallable]:
        """Get the optimizer class with any default arguments applied.

        This allows direct instantiation of optimizers with their default configs
        without going through the full factory.

        Args:
            name_or_info: Name of the optimizer
            bind_defaults: Bind default arguments to optimizer class via `partial` before returning

        Returns:
            Optimizer class or partial with defaults applied

        Raises:
            ValueError: If optimizer not found
        """
        if isinstance(name_or_info, str):
            opt_info = self.get_optimizer_info(name_or_info)
        else:
            assert isinstance(name_or_info, OptimInfo)
            opt_info = name_or_info

        if isinstance(opt_info.opt_class, str):
            # Special handling for APEX and BNB optimizers
            if opt_info.opt_class.startswith('apex.'):
                assert torch.cuda.is_available(), 'CUDA required for APEX optimizers'
                try:
                    opt_class = _import_class(opt_info.opt_class)
                except ImportError as e:
                    raise ImportError('APEX optimizers require apex to be installed') from e
            elif opt_info.opt_class.startswith('bitsandbytes.'):
                assert torch.cuda.is_available(), 'CUDA required for bitsandbytes optimizers'
                try:
                    opt_class = _import_class(opt_info.opt_class)
                except ImportError as e:
                    raise ImportError('bitsandbytes optimizers require bitsandbytes to be installed') from e
            else:
                opt_class = _import_class(opt_info.opt_class)
        else:
            opt_class = opt_info.opt_class

        # Return class or partial with defaults
        if bind_defaults and opt_info.defaults:
            opt_class = partial(opt_class, **opt_info.defaults)

        return opt_class

    def create_optimizer(
            self,
            model_or_params: Union[nn.Module, Params],
            opt: str,
            lr: Optional[float] = None,
            weight_decay: float = 0.,
            momentum: float = 0.9,
            foreach: Optional[bool] = None,
            weight_decay_exclude_1d: bool = True,
            layer_decay: Optional[float] = None,
            param_group_fn: Optional[Callable[[nn.Module], Params]] = None,
            **kwargs: Any,
    ) -> optim.Optimizer:
        """Create an optimizer instance.

        Args:
            model_or_params: Model or parameters to optimize
            opt: Name of optimizer to create
            lr: Learning rate
            weight_decay: Weight decay factor
            momentum: Momentum factor for applicable optimizers
            foreach: Enable/disable foreach operation
            weight_decay_exclude_1d: Whether to skip weight decay for 1d params (biases and norm affine)
            layer_decay: Layer-wise learning rate decay
            param_group_fn: Optional custom parameter grouping function
            **kwargs: Additional optimizer-specific arguments

        Returns:
            Configured optimizer instance

        Raises:
            ValueError: If optimizer not found or configuration invalid
        """

        # Get parameters to optimize
        if isinstance(model_or_params, nn.Module):
            # Extract parameters from a nn.Module, build param groups w/ weight-decay and/or layer-decay applied
            no_weight_decay = getattr(model_or_params, 'no_weight_decay', lambda: set())()

            if param_group_fn:
                # run custom fn to generate param groups from nn.Module
                parameters = param_group_fn(model_or_params)
            elif layer_decay is not None:
                parameters = param_groups_layer_decay(
                    model_or_params,
                    weight_decay=weight_decay,
                    layer_decay=layer_decay,
                    no_weight_decay_list=no_weight_decay,
                    weight_decay_exclude_1d=weight_decay_exclude_1d,
                )
                weight_decay = 0.
            elif weight_decay and weight_decay_exclude_1d:
                parameters = param_groups_weight_decay(
                    model_or_params,
                    weight_decay=weight_decay,
                    no_weight_decay_list=no_weight_decay,
                )
                weight_decay = 0.
            else:
                parameters = model_or_params.parameters()
        else:
            # pass parameters / parameter groups through to optimizer
            parameters = model_or_params

        # Parse optimizer name
        opt_split = opt.lower().split('_')
        opt_name = opt_split[-1]
        use_lookahead = opt_split[0] == 'lookahead' if len(opt_split) > 1 else False

        opt_info = self.get_optimizer_info(opt_name)

        # Build optimizer arguments
        opt_args: Dict[str, Any] = {'weight_decay': weight_decay, **kwargs}

        # Add LR to args, if None optimizer default is used, some optimizers manage LR internally if None.
        if lr is not None:
            opt_args['lr'] = lr

        # Apply optimizer-specific settings
        if opt_info.defaults:
            for k, v in opt_info.defaults.items():
                opt_args.setdefault(k, v)

        # timm has always defaulted momentum to 0.9 if optimizer supports momentum, keep for backward compat.
        if opt_info.has_momentum:
            opt_args.setdefault('momentum', momentum)

        # Remove commonly used kwargs that aren't always supported
        if not opt_info.has_eps:
            opt_args.pop('eps', None)
        if not opt_info.has_betas:
            opt_args.pop('betas', None)

        if foreach is not None:
            # Explicitly activate or deactivate multi-tensor foreach impl.
            # Not all optimizers support this, and those that do usually default to using
            # multi-tensor impl if foreach is left as default 'None' and can be enabled.
            opt_args.setdefault('foreach', foreach)

        # Create optimizer
        opt_class = self.get_optimizer_class(opt_info, bind_defaults=False)
        optimizer = opt_class(parameters, **opt_args)

        # Apply Lookahead if requested
        if use_lookahead:
            optimizer = Lookahead(optimizer)

        return optimizer


def _register_sgd_variants(registry: OptimizerRegistry) -> None:
    """Register SGD-based optimizers"""
    sgd_optimizers = [
        OptimInfo(
            name='sgd',
            opt_class=optim.SGD,
            description='Stochastic Gradient Descent with Nesterov momentum (default)',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='momentum',
            opt_class=optim.SGD,
            description='Stochastic Gradient Descent with classical momentum',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': False}
        ),
        OptimInfo(
            name='sgdp',
            opt_class=SGDP,
            description='SGD with built-in projection to unit norm sphere',
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='sgdw',
            opt_class=SGDW,
            description='SGD with decoupled weight decay and Nesterov momentum',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': True}
        ),
    ]
    for opt in sgd_optimizers:
        registry.register(opt)


def _register_adam_variants(registry: OptimizerRegistry) -> None:
    """Register Adam-based optimizers"""
    adam_optimizers = [
        OptimInfo(
            name='adam',
            opt_class=optim.Adam,
            description='torch.optim Adam (Adaptive Moment Estimation)',
            has_betas=True
        ),
        OptimInfo(
            name='adamw',
            opt_class=optim.AdamW,
            description='torch.optim Adam with decoupled weight decay regularization',
            has_betas=True
        ),
        OptimInfo(
            name='adamp',
            opt_class=AdamP,
            description='Adam with built-in projection to unit norm sphere',
            has_betas=True,
            defaults={'wd_ratio': 0.01, 'nesterov': True}
        ),
        OptimInfo(
            name='nadam',
            opt_class=Nadam,
            description='Adam with Nesterov momentum',
            has_betas=True
        ),
        OptimInfo(
            name='nadamw',
            opt_class=NAdamW,
            description='Adam with Nesterov momentum and decoupled weight decay',
            has_betas=True
        ),
        OptimInfo(
            name='radam',
            opt_class=RAdam,
            description='Rectified Adam with variance adaptation',
            has_betas=True
        ),
        OptimInfo(
            name='adamax',
            opt_class=optim.Adamax,
            description='torch.optim Adamax, Adam with infinity norm for more stable updates',
            has_betas=True
        ),
        OptimInfo(
            name='adafactor',
            opt_class=Adafactor,
            description='Memory-efficient implementation of Adam with factored gradients',
        ),
        OptimInfo(
            name='adafactorbv',
            opt_class=AdafactorBigVision,
            description='Big Vision variant of Adafactor with factored gradients, half precision momentum.',
        ),
        OptimInfo(
            name='adopt',
            opt_class=Adopt,
            description='Memory-efficient implementation of Adam with factored gradients',
        ),
        OptimInfo(
            name='adoptw',
            opt_class=Adopt,
            description='Memory-efficient implementation of Adam with factored gradients',
            defaults={'decoupled': True}
        ),
    ]
    for opt in adam_optimizers:
        registry.register(opt)


def _register_lamb_lars(registry: OptimizerRegistry) -> None:
    """Register LAMB and LARS variants"""
    lamb_lars_optimizers = [
        OptimInfo(
            name='lamb',
            opt_class=Lamb,
            description='Layer-wise Adaptive Moments for batch optimization',
            has_betas=True
        ),
        OptimInfo(
            name='lambc',
            opt_class=Lamb,
            description='LAMB with trust ratio clipping for stability',
            has_betas=True,
            defaults={'trust_clip': True}
        ),
        OptimInfo(
            name='lars',
            opt_class=Lars,
            description='Layer-wise Adaptive Rate Scaling',
            has_momentum=True
        ),
        OptimInfo(
            name='larc',
            opt_class=Lars,
            description='LARS with trust ratio clipping for stability',
            has_momentum=True,
            defaults={'trust_clip': True}
        ),
        OptimInfo(
            name='nlars',
            opt_class=Lars,
            description='LARS with Nesterov momentum',
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='nlarc',
            opt_class=Lars,
            description='LARS with Nesterov momentum & trust ratio clipping',
            has_momentum=True,
            defaults={'nesterov': True, 'trust_clip': True}
        ),
    ]
    for opt in lamb_lars_optimizers:
        registry.register(opt)


def _register_other_optimizers(registry: OptimizerRegistry) -> None:
    """Register miscellaneous optimizers"""
    other_optimizers = [
        OptimInfo(
            name='adabelief',
            opt_class=AdaBelief,
            description='Adapts learning rate based on gradient prediction error',
            has_betas=True,
            defaults={'rectify': False}
        ),
        OptimInfo(
            name='radabelief',
            opt_class=AdaBelief,
            description='Rectified AdaBelief with variance adaptation',
            has_betas=True,
            defaults={'rectify': True}
        ),
        OptimInfo(
            name='adadelta',
            opt_class=optim.Adadelta,
            description='torch.optim Adadelta, Adapts learning rates based on running windows of gradients'
        ),
        OptimInfo(
            name='adagrad',
            opt_class=optim.Adagrad,
            description='torch.optim Adagrad, Adapts learning rates using cumulative squared gradients',
            defaults={'eps': 1e-8}
        ),
        OptimInfo(
            name='adan',
            opt_class=Adan,
            description='Adaptive Nesterov Momentum Algorithm',
            defaults={'no_prox': False},
            has_betas=True,
            num_betas=3
        ),
        OptimInfo(
            name='adanw',
            opt_class=Adan,
            description='Adaptive Nesterov Momentum with decoupled weight decay',
            defaults={'no_prox': True},
            has_betas=True,
            num_betas=3
        ),
        OptimInfo(
            name='lion',
            opt_class=Lion,
            description='Evolved Sign Momentum optimizer for improved convergence',
            has_eps=False,
            has_betas=True
        ),
        OptimInfo(
            name='madgrad',
            opt_class=MADGRAD,
            description='Momentum-based Adaptive gradient method',
            has_momentum=True
        ),
        OptimInfo(
            name='madgradw',
            opt_class=MADGRAD,
            description='MADGRAD with decoupled weight decay',
            has_momentum=True,
            defaults={'decoupled_decay': True}
        ),
        OptimInfo(
            name='novograd',
            opt_class=NvNovoGrad,
            description='Normalized Adam with L2 norm gradient normalization',
            has_betas=True
        ),
        OptimInfo(
            name='rmsprop',
            opt_class=optim.RMSprop,
            description='torch.optim RMSprop, Root Mean Square Propagation',
            has_momentum=True,
            defaults={'alpha': 0.9}
        ),
        OptimInfo(
            name='rmsproptf',
            opt_class=RMSpropTF,
            description='TensorFlow-style RMSprop implementation, Root Mean Square Propagation',
            has_momentum=True,
            defaults={'alpha': 0.9}
        ),
    ]
    for opt in other_optimizers:
        registry.register(opt)
    registry.register_foreach_default('lion')


def _register_apex_optimizers(registry: OptimizerRegistry) -> None:
    """Register APEX optimizers (lazy import)"""
    apex_optimizers = [
        OptimInfo(
            name='fusedsgd',
            opt_class='apex.optimizers.FusedSGD',
            description='NVIDIA APEX fused SGD implementation for faster training',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='fusedadam',
            opt_class='apex.optimizers.FusedAdam',
            description='NVIDIA APEX fused Adam implementation',
            has_betas=True,
            defaults={'adam_w_mode': False}
        ),
        OptimInfo(
            name='fusedadamw',
            opt_class='apex.optimizers.FusedAdam',
            description='NVIDIA APEX fused AdamW implementation',
            has_betas=True,
            defaults={'adam_w_mode': True}
        ),
        OptimInfo(
            name='fusedlamb',
            opt_class='apex.optimizers.FusedLAMB',
            description='NVIDIA APEX fused LAMB implementation',
            has_betas=True
        ),
        OptimInfo(
            name='fusednovograd',
            opt_class='apex.optimizers.FusedNovoGrad',
            description='NVIDIA APEX fused NovoGrad implementation',
            has_betas=True,
            defaults={'betas': (0.95, 0.98)}
        ),
    ]
    for opt in apex_optimizers:
        registry.register(opt)


def _register_bnb_optimizers(registry: OptimizerRegistry) -> None:
    """Register bitsandbytes optimizers (lazy import)"""
    bnb_optimizers = [
        OptimInfo(
            name='bnbsgd',
            opt_class='bitsandbytes.optim.SGD',
            description='bitsandbytes SGD',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='bnbsgd8bit',
            opt_class='bitsandbytes.optim.SGD8bit',
            description='bitsandbytes 8-bit SGD with dynamic quantization',
            has_eps=False,
            has_momentum=True,
            defaults={'nesterov': True}
        ),
        OptimInfo(
            name='bnbadam',
            opt_class='bitsandbytes.optim.Adam',
            description='bitsandbytes Adam',
            has_betas=True
        ),
        OptimInfo(
            name='bnbadam8bit',
            opt_class='bitsandbytes.optim.Adam',
            description='bitsandbytes 8-bit Adam with dynamic quantization',
            has_betas=True
        ),
        OptimInfo(
            name='bnbadamw',
            opt_class='bitsandbytes.optim.AdamW',
            description='bitsandbytes AdamW',
            has_betas=True
        ),
        OptimInfo(
            name='bnbadamw8bit',
            opt_class='bitsandbytes.optim.AdamW',
            description='bitsandbytes 8-bit AdamW with dynamic quantization',
            has_betas=True
        ),
        OptimInfo(
            'bnblion',
            'bitsandbytes.optim.Lion',
            description='bitsandbytes Lion',
            has_betas=True
        ),
        OptimInfo(
            'bnblion8bit',
            'bitsandbytes.optim.Lion8bit',
            description='bitsandbytes 8-bit Lion with dynamic quantization',
            has_betas=True
        ),
        OptimInfo(
            'bnbademamix',
            'bitsandbytes.optim.AdEMAMix',
            description='bitsandbytes AdEMAMix',
            has_betas=True,
            num_betas=3,
        ),
        OptimInfo(
            'bnbademamix8bit',
            'bitsandbytes.optim.AdEMAMix8bit',
            description='bitsandbytes 8-bit AdEMAMix with dynamic quantization',
            has_betas=True,
            num_betas=3,
        ),
    ]
    for opt in bnb_optimizers:
        registry.register(opt)


default_registry = OptimizerRegistry()

def _register_default_optimizers() -> None:
    """Register all default optimizers to the global registry."""
    # Register all optimizer groups
    _register_sgd_variants(default_registry)
    _register_adam_variants(default_registry)
    _register_lamb_lars(default_registry)
    _register_other_optimizers(default_registry)
    _register_apex_optimizers(default_registry)
    _register_bnb_optimizers(default_registry)

    # Register aliases
    default_registry.register_alias('nesterov', 'sgd')
    default_registry.register_alias('nesterovw', 'sgdw')


# Initialize default registry
_register_default_optimizers()

# Public API

def list_optimizers(
        filter: str = '',
        exclude_filters: Optional[List[str]] = None,
        with_description: bool = False,
) -> List[Union[str, Tuple[str, str]]]:
    """List available optimizer names, optionally filtered.
    """
    return default_registry.list_optimizers(filter, exclude_filters, with_description)


def get_optimizer_class(
        name: str,
        bind_defaults: bool = False,
) -> Union[Type[optim.Optimizer], OptimizerCallable]:
    """Get optimizer class by name with any defaults applied.
    """
    return default_registry.get_optimizer_class(name, bind_defaults=bind_defaults)


def create_optimizer_v2(
        model_or_params: Union[nn.Module, Params],
        opt: str = 'sgd',
        lr: Optional[float] = None,
        weight_decay: float = 0.,
        momentum: float = 0.9,
        foreach: Optional[bool] = None,
        filter_bias_and_bn: bool = True,
        layer_decay: Optional[float] = None,
        param_group_fn: Optional[Callable[[nn.Module], Params]] = None,
        **kwargs: Any,
) -> optim.Optimizer:
    """Create an optimizer instance using the default registry."""
    return default_registry.create_optimizer(
        model_or_params,
        opt=opt,
        lr=lr,
        weight_decay=weight_decay,
        momentum=momentum,
        foreach=foreach,
        weight_decay_exclude_1d=filter_bias_and_bn,
        layer_decay=layer_decay,
        param_group_fn=param_group_fn,
        **kwargs
    )


def optimizer_kwargs(cfg):
    """ cfg/argparse to kwargs helper
    Convert optimizer args in argparse args or cfg like object to keyword args for updated create fn.
    """
    kwargs = dict(
        opt=cfg.opt,
        lr=cfg.lr,
        weight_decay=cfg.weight_decay,
        momentum=cfg.momentum,
    )
    if getattr(cfg, 'opt_eps', None) is not None:
        kwargs['eps'] = cfg.opt_eps
    if getattr(cfg, 'opt_betas', None) is not None:
        kwargs['betas'] = cfg.opt_betas
    if getattr(cfg, 'layer_decay', None) is not None:
        kwargs['layer_decay'] = cfg.layer_decay
    if getattr(cfg, 'opt_args', None) is not None:
        kwargs.update(cfg.opt_args)
    if getattr(cfg, 'opt_foreach', None) is not None:
        kwargs['foreach'] = cfg.opt_foreach
    return kwargs


def create_optimizer(args, model, filter_bias_and_bn=True):
    """ Legacy optimizer factory for backwards compatibility.
    NOTE: Use create_optimizer_v2 for new code.
    """
    return create_optimizer_v2(
        model,
        **optimizer_kwargs(cfg=args),
        filter_bias_and_bn=filter_bias_and_bn,
    )

