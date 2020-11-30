from .utils import SchemaBuilder
from .port_event import build as build_port_event_schema

def build():

    builder = SchemaBuilder()
    builder.add("visit_id", "STRING",
        description="Unique ID for this visit")
    builder.add("track_id", "STRING",
        description="`track_id` of the track this visit was found on")
    builder.add("ssvid", "STRING",
        description="`ssvid` of the vessel involved in the visit."
                    "N.B. Some `ssvid` may be associated with multiple tracks")
    builder.add("start_timestamp", "TIMESTAMP",
        description="timestamp at which vessel crossed into the anchorage")
    builder.add("start_lat", "FLOAT",
        description="latitude of vessel at `start_timestamp`")
    builder.add("start_lon", "FLOAT",
        description="longitude of vessel at `start_timestamp`")
    builder.add("start_anchorage_id", "STRING",
        description="`anchorage_id` of anchorage where vessel entered port")
    builder.add("end_timestamp", "TIMESTAMP",
        description="timestamp at which vessel crossed out the anchorage")
    builder.add("end_lat", "FLOAT",
        description="latitude of vessel at `end_timestamp`")
    builder.add("end_lon", "FLOAT")
    builder.add("end_anchorage_id", "STRING",
        description="longitude of vessel at `end_timestamp`")
    builder.add("events", mode="REPEATED", 
        schema_type=build_port_event_schema().fields,
        description="sequence of port events that occurred during visit"
    )

    return builder.schema
