# DO NOT EDIT! This file was generated by jschema_to_python version 0.0.1.dev29,
# with extension for dataclasses and type annotation.

from __future__ import annotations

import dataclasses
from typing import List, Literal, Optional

from torch.onnx._internal.diagnostics.infra.sarif import (
    _external_properties,
    _property_bag,
    _run,
)


@dataclasses.dataclass
class SarifLog(object):
    """Static Analysis Results Format (SARIF) Version 2.1.0 JSON Schema: a standard format for the output of static analysis tools."""

    runs: list[_run.Run] = dataclasses.field(metadata={"schema_property_name": "runs"})
    version: Literal["2.1.0"] = dataclasses.field(
        metadata={"schema_property_name": "version"}
    )
    schema_uri: Optional[str] = dataclasses.field(
        default=None, metadata={"schema_property_name": "$schema"}
    )
    inline_external_properties: Optional[
        list[_external_properties.ExternalProperties]
    ] = dataclasses.field(
        default=None, metadata={"schema_property_name": "inlineExternalProperties"}
    )
    properties: Optional[_property_bag.PropertyBag] = dataclasses.field(
        default=None, metadata={"schema_property_name": "properties"}
    )


# flake8: noqa
