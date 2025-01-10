# DO NOT EDIT! This file was generated by jschema_to_python version 0.0.1.dev29,
# with extension for dataclasses and type annotation.

from __future__ import annotations

import dataclasses
from typing import List, Optional

from torch.onnx._internal.diagnostics.infra.sarif import (
    _location_relationship,
    _logical_location,
    _message,
    _physical_location,
    _property_bag,
    _region,
)


@dataclasses.dataclass
class Location(object):
    """A location within a programming artifact."""

    annotations: Optional[list[_region.Region]] = dataclasses.field(
        default=None, metadata={"schema_property_name": "annotations"}
    )
    id: int = dataclasses.field(default=-1, metadata={"schema_property_name": "id"})
    logical_locations: Optional[list[_logical_location.LogicalLocation]] = (
        dataclasses.field(
            default=None, metadata={"schema_property_name": "logicalLocations"}
        )
    )
    message: Optional[_message.Message] = dataclasses.field(
        default=None, metadata={"schema_property_name": "message"}
    )
    physical_location: Optional[_physical_location.PhysicalLocation] = (
        dataclasses.field(
            default=None, metadata={"schema_property_name": "physicalLocation"}
        )
    )
    properties: Optional[_property_bag.PropertyBag] = dataclasses.field(
        default=None, metadata={"schema_property_name": "properties"}
    )
    relationships: Optional[list[_location_relationship.LocationRelationship]] = (
        dataclasses.field(
            default=None, metadata={"schema_property_name": "relationships"}
        )
    )


# flake8: noqa
