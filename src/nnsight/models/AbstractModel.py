from __future__ import annotations

import copy
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Union

import accelerate
import torch
from torch.utils.hooks import RemovableHandle

from ..alteration import REPOID_TO_ALTERATION
from ..contexts.Generator import Generator
from ..contexts.Runner import Runner
from ..editing.Editor import Edit, Editor
from ..editing.GraphEdit import GraphEdit
from ..editing.WrapperModuleEdit import WrapperModuleEdit
from ..fx.Graph import Graph
from ..intervention import HookModel, intervene
from ..logger import logger
from ..Module import Module
from ..patching import Patcher


class AbstractModel(ABC):
    """Abstract class to be implmented for pytorch models wishing to gain this package's functionality.

    Attributes:
        repoid_or_path (str): Hugging face repo id of model to load or path to checkpoint.
        args (List[Any]): __desc__
        kwargs (Dict[str,Any]): __desc__
    """

    def __init__(
        self,
        repoid_path_model: Union[str, torch.nn.Module],
        *args: List[Any],
        alter: bool = True,
        tokenizer=None,
        **kwargs: Dict[str, Any],
    ) -> None:
        super().__init__()

        self.repoid_path_clsname = repoid_path_model
        self.args = args
        self.kwargs = kwargs
        # Boolean on whether to check if alterations exist for this module and apply them.
        self.alter = alter
        # Boolean on whether this model has been dispatched (locally loaded) yet
        self.dispatched = False
        self.custom_model = False
        self.local_model: torch.nn.Module = None
        self.tokenizer = tokenizer
        self.edits: List[Edit] = list()

        if isinstance(repoid_path_model, torch.nn.Module):
            self.repoid_path_clsname = repoid_path_model.__class__.__name__
            self.custom_model = True
            self.dispatched = True
            self.local_model = repoid_path_model

        logger.debug(f"Initializing `{self.repoid_path_clsname}`...")

        # If alter and alteration exist, use alteration patcher context while loading module.
        with self.alteration() if self.alter else Patcher():
            # Use accelerate and .to('meta') to assure tensors are loaded to 'meta' device
            with accelerate.init_empty_weights(include_buffers=True):
                if self.custom_model:
                    self.meta_model: Module = Module.wrap(
                        copy.deepcopy(self.local_model).to("meta")
                    )
                else:
                    self.meta_model: Module = Module.wrap(
                        self._load_meta(self.repoid_path_clsname, *args, **kwargs).to(
                            "meta"
                        )
                    )

        # Wrap all modules in our Module class.
        for name, module in self.meta_model.named_children():
            module = Module.wrap(module)

            setattr(self.meta_model, name, module)

        # Set module_path attribute so Modules know their place.
        for name, module in self.meta_model.named_modules():
            module.module_path = name

        # Run inital dummy string to populate Module shapes, dtypes etc
        self._run_meta("_")

        logger.debug(f"Initialized `{self.repoid_path_clsname}`")

    def __repr__(self) -> str:
        return repr(self.meta_model)

    def __getattr__(self, key) -> Any:
        """Allows access of sub-modules on meta_model directly from AbstractModel object

        Args:
            key (_type_): _description_

        Returns:
            Any: _description_
        """
        return getattr(self.meta_model, key)

    def __call__(
        self,
        fn: Callable,
        inputs: Any,
        graph: Graph,
        *args,
        edits: List[Edit] = None,
        inference: bool = True,
        **kwargs,
    ) -> Any:
        """Runs some function with some inputs and some graph with the approriate context for this model.

        Args:
            fn (Callable): _description_
            inputs (Any): _description_
            graph (Graph): _description_
            edits (List[Edit], optional): _description_. Defaults to None.
            inference (bool, optional): _description_. Defaults to True.

        Returns:
            Any: _description_
        """
        if edits is None:
            edits = self.edits

        # If local_model not yet loaded, do so.
        if not self.dispatched:
            with self.alteration() if self.alter else Patcher():
                self.local_model = self._load_local(
                    self.repoid_path_clsname, *self.args, **self.kwargs
                )

                # By default, all params should be frozen.
                for param in self.local_model.parameters():
                    param.requires_grad = False

        with Editor(self, edits):
            # Send local_model to graph to re-compile
            graph.compile(self.local_model)

            increment_hook = self._register_increment_hook(
                lambda module, input, output: graph.increment()
            )

            # The intervention graph for running a Model will have the modules that are involved
            # in the graph's argument_node_names.
            modules = set(
                [
                    ".".join(name.split(".")[:-2])
                    for name in graph.argument_node_names.keys()
                ]
            )

            logger.debug(f"Running `{self.repoid_path_clsname}`...")

            self.local_model.eval() if inference else self.local_model.train()

            with torch.inference_mode(mode=inference):
                with HookModel(
                    self.local_model,
                    list(modules),
                    input_hook=lambda activations, module_path: intervene(
                        activations, module_path, graph, "input"
                    ),
                    output_hook=lambda activations, module_path: intervene(
                        activations, module_path, graph, "output"
                    ),
                ):
                    output = fn(inputs, *args, **kwargs)

            increment_hook.remove()

            logger.debug(f"Completed `{self.repoid_path_clsname}`")

        return output

    def alteration(self) -> Patcher:
        return REPOID_TO_ALTERATION.get(self.repoid_path_clsname, Patcher())

    def generate(self, *args, **kwargs) -> Generator:
        return Generator(self, *args, **kwargs)

    def forward(self, inputs, *args, **kwargs) -> Runner:
        return Runner(inputs, self, *args, **kwargs)

    def modulize(self, module: Module, node_name: str, module_name: str) -> None:
        """_summary_

        Args:
            module (Module): _description_
            node_name (str): _description_
            module_name (str): _description_
        """

        # Create a WrapperModuleEdit which just adds a WrapperModule to an existing module at the given moduel_name.
        wme = WrapperModuleEdit(module.module_path, module_name)
        # Wrap with our Module and update new attributes.
        wme.wrapper: Module = Module.wrap(wme.wrapper)
        wme.wrapper.module_path = f"{module.module_path}.{module_name}"
        wme.wrapper.tracer = module.tracer
        wme.wrapper.output_shape = module.output_shape
        # Carry out the edit on the meta_model.
        wme.edit(self.meta_model)

        # Get/create the execution graph for the module's forward method.
        graph = module.graph

        # Add two proxies/nodes, one to get the new WrapperModule we added and another to call it with the data from the original module.
        # Passing the data through the wrapper module allows hooking of the module's output like usual.
        module_proxy = getattr(graph.module_proxy, module_name)
        module_proxy(graph.nodes[node_name])

        # Create and carry out the edit on the meta_model.
        ge = GraphEdit(module.module_path, module.graph)
        ge.edit(self.meta_model)

        # Append to self.edits so when we call the local model, we temporarily edit the module in the same way as the meta model.
        self.edits.append(wme)
        self.edits.append(ge)

    @abstractmethod
    def _prepare_inputs(self, inputs: Any, **kwargs) -> Any:
        """Abstract method for Model type to process inputs.

        Args:
            inputs (Any): _description_

        Returns:
            Any: _description_
        """
        raise NotImplementedError()

    @abstractmethod
    def _load_meta(self, repoid_or_path, *args, **kwargs) -> torch.nn.Module:
        """Abstract method for Model type to initialize what it needs for it's meta model.

        Args:
            repoid_or_path (_type_): _description_

        Returns:
            torch.nn.Module: _description_
        """
        raise NotImplementedError()

    @abstractmethod
    def _load_local(self, repoid_or_path, *args, **kwargs) -> torch.nn.Module:
        raise NotImplementedError()

    @abstractmethod
    def _run_meta(self, inputs, *args, **kwargs) -> Any:
        raise NotImplementedError()

    @abstractmethod
    def _run_local(self, inputs, *args, **kwargs) -> Any:
        raise NotImplementedError()

    @abstractmethod
    def _generation(self, inputs, *args, **kwargs) -> Any:
        raise NotImplementedError()

    @abstractmethod
    def _register_increment_hook(self, hook) -> RemovableHandle:
        raise NotImplementedError()
