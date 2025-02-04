# mypy: allow-untyped-defs
"""
This module dispatches the graphs to either the forward-only or joint compilation
pathways, taking into account the AOTConfig and the collected ViewAndMutationMetadata.
"""

import dataclasses
import functools
import inspect
from typing import Any, Callable, NamedTuple, Optional, Sequence

import torch
import torch.utils._pytree as pytree
import torch.utils.dlpack
from torch import Tensor
from torch._dispatch.python import enable_python_dispatcher
from torch._dynamo.utils import detect_fake_mode, lazy_format_graph_code
from torch._logging import getArtifactLogger, trace_structured
from torch._subclasses.functional_tensor import FunctionalTensorMode
from torch.fx.experimental.proxy_tensor import make_fx
from torchgen.utils import dataclass_repr

from .. import config
from .functional_utils import (
    assert_functional_graph,
    propagate_input_mutation_stacktraces,
)
from .schemas import AOTConfig, SubclassMeta, SubclassTracingInfo, ViewAndMutationMeta
from .traced_function_transforms import (
    aot_dispatch_subclass,
    create_functionalized_fn,
    create_joint,
    fn_input_mutations_to_outputs,
    fn_prepped_for_autograd,
    handle_effect_tokens_fn,
)
from .utils import (
    copy_fwd_metadata_to_bw_nodes,
    register_buffer_assignment_hook,
    root_module_when_exporting_non_strict,
    unlift_tokens,
)


aot_graphs_log = getArtifactLogger(__name__, "aot_graphs")


def _create_graph(f, args, *, aot_config: AOTConfig) -> torch.fx.GraphModule:
    # FunctionalTensorMode must be enabled here.
    # See Note [Accessing .grad_fn on FunctionalTensor]
    with enable_python_dispatcher(), FunctionalTensorMode(
        pre_dispatch=aot_config.pre_dispatch,
        export=aot_config.is_export,
        # Allow token discovery for joint fn tracing as tokens can be used in backward.
        _allow_token_discovery=True,
    ):
        fx_g = make_fx(
            f,
            decomposition_table=aot_config.decompositions,
            record_module_stack=True,
            pre_dispatch=aot_config.pre_dispatch,
        )(*args)

    return fx_g


# TODO: Refactor the following code so detach() persists item_memo
def _detach_and_copy_item_memo(t):
    detached_t = t.detach()
    if hasattr(t, "item_memo"):
        detached_t.item_memo = t.item_memo
    return detached_t


def aot_dispatch_base_graph(
    flat_fn,
    flat_args: list[Tensor],
    aot_config: AOTConfig,
    *,
    fw_metadata: ViewAndMutationMeta,
) -> tuple[torch.fx.GraphModule, list[Any], Optional[SubclassMeta]]:
    # aot_dispatch_base requires functionalization, but doesn't need to handle as many cases as the autograd case.
    # The cases that aot_dispatch_base doesn't need to handle include:
    # - outputs that are aliases of graph intermediates
    # - outputs that are aliases of graph inputs
    # While cases that it does need to handle include:
    # - input mutations (including when inputs are aliases of each other)
    # - input metadata mutations
    state, subclass_tracing_info = _aot_dispatch_function_wrapper(
        flat_fn,
        flat_args,
        [fn_input_mutations_to_outputs],
        aot_config=aot_config,
        is_joint_structure=False,
        meta=fw_metadata,
        trace_joint=False,
    )

    fn_to_trace, updated_flat_args_subclasses_desugared = state
    maybe_subclass_meta = subclass_tracing_info[-1] if subclass_tracing_info else None

    aot_graphs_log.debug(
        "aot_config id: %s, fw_metadata=%s,subclass_metadata=%s",
        str(aot_config.aot_id),
        str(fw_metadata),
        str(maybe_subclass_meta),
    )

    # We track buffer assignments when exporting in non-strict mode.
    # (In contrast, strict mode errors on any attribute assignment.)
    mod_when_exporting_non_strict = root_module_when_exporting_non_strict(flat_fn)
    if aot_config.is_export and mod_when_exporting_non_strict is not None:
        # For any buffer that is assigned, we want to associate it to the final proxy node
        # that it is assigned to. This node can then be added as a buffer mutation output.
        assigned_buffers: dict[str, str] = {}
        hook = register_buffer_assignment_hook(
            mod_when_exporting_non_strict, assigned_buffers
        )

    fake_mode = detect_fake_mode()
    if fake_mode:
        saved_updated_flat_args_subclasses_desugared = pytree.tree_map_only(
            torch.Tensor,
            _detach_and_copy_item_memo,
            updated_flat_args_subclasses_desugared,
        )
    else:
        saved_updated_flat_args_subclasses_desugared = pytree.tree_map_only(
            torch.Tensor, lambda t: t.detach(), updated_flat_args_subclasses_desugared
        )

    fw_module = _create_graph(
        fn_to_trace,
        updated_flat_args_subclasses_desugared,
        aot_config=aot_config,
    )

    if aot_config.is_export and mod_when_exporting_non_strict is not None:
        # We update metadata to consider any assigned buffers as buffer mutations.
        i = len(dict(mod_when_exporting_non_strict.named_parameters()))
        for name, _ in mod_when_exporting_non_strict.named_buffers():
            if name in assigned_buffers and not fw_metadata.input_info[i].mutates_data:  # type: ignore[possibly-undefined]
                fw_metadata.input_info[i] = dataclasses.replace(
                    fw_metadata.input_info[i], mutates_data=True
                )
                fw_metadata.num_mutated_inp_runtime_indices += 1
            i += 1

        # We add nodes corresponding to buffer assignments as output nodes in the graph.
        add_nodes = []
        output_node = list(fw_module.graph.nodes)[-1]
        for name in assigned_buffers.values():  # type: ignore[possibly-undefined]
            for node in fw_module.graph.nodes:
                if node.name == name:
                    add_nodes.append(node)
                    node.users[output_node] = None
        output_node.args = ((*add_nodes, *output_node.args[0]),)

        hook.remove()  # type: ignore[possibly-undefined]

    # As long as we opted to remove input mutations, then
    # there should be *NO* mutating ops in the graph at this point.
    copy_count = assert_functional_graph(fw_module.graph)
    fw_module.graph.eliminate_dead_code()
    fw_module.recompile()

    copy_count2 = assert_functional_graph(fw_module.graph)
    propagate_input_mutation_stacktraces(fw_module.graph)

    # See Note [Side-Effectful Tokens in AOTAutograd]
    num_tokens = len(fw_metadata.tokens)
    if num_tokens != 0 and config.unlift_effect_tokens:
        unlift_tokens(fw_module, fw_metadata, aot_config)
        saved_updated_flat_args_subclasses_desugared = (
            saved_updated_flat_args_subclasses_desugared[num_tokens:]
        )

    assert copy_count == copy_count2

    if aot_config.enable_log:
        aot_graphs_log.info(
            "%s",
            lazy_format_graph_code(
                "Forward graph",
                fw_module,
                aot_config.aot_id,
                include_stride=True,
                include_device=True,
                colored=True,
            ),
        )

        trace_structured(
            "artifact",
            metadata_fn=lambda: {
                "name": "aot_forward_graph_fw_metadata",
                "encoding": "string",
            },
            payload_fn=lambda: dataclass_repr(fw_metadata),
        )
        if maybe_subclass_meta is not None:
            trace_structured(
                "artifact",
                metadata_fn=lambda: {
                    "name": "aot_forward_graph_fw_subclass_metadata",
                    "encoding": "string",
                },
                payload_fn=lambda: dataclass_repr(maybe_subclass_meta),
            )

        trace_structured(
            "aot_inference_graph",
            payload_fn=lambda: fw_module.print_readable(
                print_output=False, include_stride=True, include_device=True
            ),
        )

    # TODO: should factor this into a separate function for export that always only returns just the graph.
    if aot_config.is_export:
        assert (
            maybe_subclass_meta is None
        ), "aot_export_module does not support tensor subclass inputs for now."

    assert isinstance(saved_updated_flat_args_subclasses_desugared, list)
    return fw_module, saved_updated_flat_args_subclasses_desugared, maybe_subclass_meta


# Has the precondition that there
# are no duplicate arguments in flat_args (e.g., the same Tensor
# object never shows up twice.  However, two tensor inputs MAY alias
# the same storage, so long as they have separate TensorImpls.)
def aot_dispatch_autograd_graph(
    flat_fn,
    flat_args: list[Any],
    aot_config: AOTConfig,
    *,
    fw_metadata: ViewAndMutationMeta,
) -> tuple[torch.fx.GraphModule, tuple[list[Any], list[Any]], Optional[SubclassMeta]]:
    # traced_tangents corresponds to the set of outputs in the traced forward that should get grad_outputs in the traced backward.
    # It includes outputs of the original forward, *and* any updated inputs due to input mutations.
    # However, it does *not* include any outputs that are aliases of inputs or intermediates, or any metadata-only input mutations.

    state, subclass_tracing_info = _aot_dispatch_function_wrapper(
        flat_fn,
        flat_args,
        [fn_prepped_for_autograd, create_joint],
        aot_config=aot_config,
        functionalized_inputs=[flat_args, fw_metadata.traced_tangents],
        is_joint_structure=True,
        meta=fw_metadata,
        trace_joint=True,
    )
    joint_fn_to_trace, updated_joint_inputs = state

    # When we call _create_graph, this may mutate the metadata of joint
    # inputs.  But callers are expecting to get the original joint inputs.  So
    # we make aliases of all the inputs to make sure we have a copy that
    # doesn't get modified.
    #
    # This destroys requires_grad/grad_fn information.  However, backends
    # beneath AOTAutograd are indifferent to this information, so it doesn't
    # matter.

    fake_mode = detect_fake_mode()
    if fake_mode:
        saved_updated_joint_inputs = pytree.tree_map_only(
            torch.Tensor, _detach_and_copy_item_memo, updated_joint_inputs
        )
    else:
        saved_updated_joint_inputs = pytree.tree_map_only(
            torch.Tensor, lambda t: t.detach(), updated_joint_inputs
        )
    maybe_subclass_meta = (
        subclass_tracing_info.maybe_subclass_meta if subclass_tracing_info else None
    )

    fx_g = _create_graph(joint_fn_to_trace, updated_joint_inputs, aot_config=aot_config)

    # There should be *NO* mutating ops in the graph at this point.
    assert_functional_graph(fx_g.graph)

    # Redundant with the check above, but worth having in case tracing introduced
    # a fake tensor. Unlikely.
    # See Note: [Fake Modules and AOTAutograd]
    torch._dynamo.utils.assert_no_fake_params_or_buffers(fx_g)
    fx_g.graph.eliminate_dead_code()
    copy_fwd_metadata_to_bw_nodes(fx_g)
    fx_g.recompile()

    # TODO: in AOTAutograd, we create metadata like _indices_of_inps_to_detach to detect
    # when we need to manually detach() some inputs in the forward.
    # Higher order ops might eventually need to do the same.
    if aot_config.is_export:
        assert (
            maybe_subclass_meta is None
        ), "aot_export_module does not support tensor subclass inputs for now."
    return fx_g, saved_updated_joint_inputs, maybe_subclass_meta


def _aot_dispatch_function_wrapper(
    fn: Callable[..., Any],
    args: list[Tensor],
    wrappers: Sequence[Callable[..., Any]],
    *,
    aot_config: AOTConfig,
    functionalized_inputs: Optional[Sequence[Any]] = None,
    **kwargs: Any,
) -> tuple["FuncArgs", Optional[SubclassTracingInfo]]:
    """Wrap a function with a list of tensor arguments"""
    subclass_tracing_info: SubclassTracingInfo | None = None

    @functools.wraps(aot_dispatch_subclass)
    def _aot_dispatch_subclass(
        fn: Callable[..., Any],
        args: list[Tensor],
        **kwargs: Any,
    ) -> tuple[Callable[..., Any], list[Tensor]]:
        # TODO(rec): make this purely functional
        nonlocal subclass_tracing_info
        subclass_tracing_info = aot_dispatch_subclass(fn, args, **kwargs)
        return subclass_tracing_info[:2]

    @functools.wraps(create_functionalized_fn)
    def _create_functionalized_fn(
        fn: Callable[..., Any],
        args: list[Tensor],
        **kwargs: Any,
    ) -> tuple[Callable[..., Any], list[Tensor]]:
        # TODO(rec): the functionalized_inputs logic faithfully represents
        # existing behavior but the reasons behind it are unclear.
        inputs = functionalized_inputs or args
        return create_functionalized_fn(fn, inputs, **kwargs)

    steps = WrapperSteps(
        *wrappers,
        _create_functionalized_fn,
        _aot_dispatch_subclass,
        handle_effect_tokens_fn,
    )
    # TODO: replace with AOTDispatchSubclassWrapper once we refactor
    # fn_input_mutations_to_outputs and create_functionalized_fn
    # into CompilerWrappers.

    func_args = steps.run(
        FuncArgs(fn, args),
        aot_config=aot_config,
        fw_only=fn,
        keep_data_input_mutations=aot_config.keep_inference_input_mutations,
        **kwargs,
    )
    return func_args, subclass_tracing_info


class FuncArgs(NamedTuple):
    fn: Callable[..., Any]
    args: Sequence[Tensor]


class Wrapper:
    def __init__(self, wrapper_fn: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        self.wrapper_fn = wrapper_fn
        self.kwargs = kwargs
        self._params = frozenset(inspect.signature(self.wrapper_fn).parameters)

    def __call__(self, func_args: FuncArgs, **kwargs: Any) -> FuncArgs:
        """Wrap a FuncArgs to produce new FuncArgs"""
        kwargs["args"] = func_args.args
        kwargs = {k: v for k, v in kwargs.items() if k in self._params}

        r = self.wrapper_fn(func_args.fn, **kwargs, **self.kwargs)
        if isinstance(r, tuple):
            assert len(r) == 2, str(r)
            fn, args = r
        else:
            fn, args = r, func_args.args

        assert callable(fn)
        assert isinstance(args, Sequence)

        return FuncArgs(fn, args)


class WrapperSteps:
    """A sequence of wrappers that operate recursively on a FuncArgs"""

    def __init__(self, *steps: Callable[..., Any], **kwargs: Any) -> None:
        self.steps = [Wrapper(s, kwargs) for s in steps]

    def run_all(self, func_args: FuncArgs, **kwargs: Any) -> list[FuncArgs]:
        return [f := func_args] + [f := step(f, **kwargs) for step in self.steps]

    def run(self, func_args: FuncArgs, **kwargs: Any) -> FuncArgs:
        return self.run_all(func_args, **kwargs)[-1]
