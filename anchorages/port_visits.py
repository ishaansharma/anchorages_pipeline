from __future__ import absolute_import, print_function, division

import argparse
import logging
import re
import ujson as json
import json as classic_json
import datetime
from collections import namedtuple, Counter, defaultdict
import itertools as it
import os
import math
import s2sphere
from .port_name_filter import normalized_valid_names
from .union_find import UnionFind
from .sparse_inland_mask import SparseInlandMask
from .distance import distance
from .nearest_port import port_finder, AnchorageFinder, BUFFER_KM as VISIT_BUFFER_KM, Port

# TODO put unit reg in package if we refactor
# import pint
# unit = pint.UnitRegistry()

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.metrics import Metrics
from apache_beam.metrics.metric import MetricsFilter
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions


from  . import common as cmn
from .transforms.source import Source

PseudoAnchorage = namedtuple("PseudoAnchorage", ['mean_location', "s2id", "port_name"])

def PseudoAnchorage_from_json(obj):
    return PseudoAnchorage(cmn.LatLon(obj['anchor_lat'], obj['anchor_lon']), obj['anchor_id'], (obj['FINAL_NAME'], obj['iso3']))


IN_PORT = "IN_PORT"
AT_SEA = "AT_SEA"



# TODO: make into real class with constructor.
VisitEvent = namedtuple("VisitEvent", 
    ['anchorage_id', 'lat', 'lon', 'mmsi', 'timestamp', 'port_label', 'event_type'])


# Questions:
# Should we call lat/lon, anchorage_lat, anchorage_lon

def find_in_out_events((md, (s2ids, records)), port_entry_dist, port_exit_dist, anchorage_map):
    state = None
    current_port = None
    anchorages = [anchorage_map[x] for x in s2ids if x in anchorage_map]
    # return [(len(anchorages), len(anchorage_map), anchorage_map.keys()[:1], list(s2ids)[:1])]
    finder = AnchorageFinder(anchorages)
    events = []
    buffer_dist = max(port_entry_dist, port_exit_dist)
    for rcd in records:
        port, dist = finder.is_within_dist(buffer_dist, rcd)
        if port is not None and dist < port_entry_dist:
            # We are in a port
            if state == AT_SEA:
                # We were outside of a port; so entered a port
                events.append(VisitEvent(anchorage_id=port.anchorage_point.s2id, 
                                         lat=port.lat, 
                                         lon=port.lon, 
                                         mmsi=md, 
                                         timestamp=rcd.timestamp, 
                                         port_label=port.name, 
                                         event_type="PORT_ENTRY")) 
            current_port = port
            state = IN_PORT
        elif port is None or dist > port_exit_dist:
            # We are outside a port
            if state == IN_PORT:
                # We were in a port, so exited a port
                events.append(VisitEvent(anchorage_id=current_port.anchorage_point.s2id, 
                                         lat=current_port.lat, 
                                         lon=current_port.lon, 
                                         mmsi=md, 
                                         timestamp=rcd.timestamp, 
                                         port_label=current_port.name, 
                                         event_type="PORT_EXIT")) 
            state = AT_SEA
            current_port = None
    return events



def event_to_json(visit):
    visit = visit._replace(timestamp = cmn.datetime_to_text(visit.timestamp))
    return json.dumps(visit._asdict())





class CreateVesselRecords(beam.PTransform):

    def __init__(self, blacklisted_mmsis):
        self.blacklisted_mmsis = blacklisted_mmsis

    def from_msg(self, msg):
        obj = cmn.VesselRecord.from_msg(msg)
        if (obj is None) or (obj[0] in self.blacklisted_mmsis):
            return []
        else:
            return [obj]

    def expand(self, ais_source):
        return (ais_source
            | beam.FlatMap(self.from_msg)
        )


class CreateTaggedPaths(beam.PTransform):

    def __init__(self, min_required_positions):
        self.min_required_positions = min_required_positions
        self.FIVE_MINUTES = datetime.timedelta(minutes=5)

    def order_by_timestamp(self, item):
        mmsi, records = item
        records = list(records)
        records.sort(key=lambda x: x.timestamp)
        return mmsi, records

    def dedup_by_timestamp(self, item):
        key, source = item
        seen = set()
        sink = []
        for x in source:
            if x.timestamp not in seen:
                sink.append(x)
                seen.add(x.timestamp)
        return (key, sink)

    def long_enough(self, item):
        mmsi, records = item
        return len(records) >= self.min_required_positions

    def thin_records(self, item):
        mmsi, records = item
        last_timestamp = datetime.datetime(datetime.MINYEAR, 1, 1)
        thinned = []
        for rcd in records:
            if (rcd.timestamp - last_timestamp) >= self.FIVE_MINUTES:
                last_timestamp = rcd.timestamp
                thinned.append(rcd)
        return mmsi, thinned

    def tag_records(self, item):
        mmsi, records = item
        # TODO: reimplement that here
        return (mmsi, cmn.tag_with_destination_and_id(records))

    def tag_path(self, item):
        mmsi, records = item
        s2ids = set()
        for rcd in records:
            s2_cell_id = s2sphere.CellId.from_token(rcd.s2id).parent(cmn.VISITS_S2_SCALE)
            s2ids.add(s2_cell_id.to_token())
            for cell_id in s2_cell_id.get_all_neighbors(cmn.VISITS_S2_SCALE):
                s2ids.add(cell_id.to_token())
        return (mmsi, (s2ids, records))

    def expand(self, vessel_records):
        return (vessel_records
            | beam.GroupByKey()
            | beam.Map(self.order_by_timestamp)
            | beam.Map(self.dedup_by_timestamp)
            | beam.Filter(self.long_enough)
            | beam.Map(self.thin_records)
            | beam.Map(self.tag_records)
            | beam.Map(self.tag_path)
            )




ais_query = """
SELECT mmsi, lat, lon, timestamp, destination FROM   
  TABLE_DATE_RANGE([world-fishing-827:{table}.], 
                    TIMESTAMP('{start:%Y-%m-%d}'), TIMESTAMP('{end:%Y-%m-%d}')) 
"""

def run(argv=None):
    """Main entry point; defines and runs the wordcount pipeline.
    """

    parser = argparse.ArgumentParser()

    parser.add_argument('--name', required=True, help='Name to prefix output and job name if not otherwise specified')

    parser.add_argument('--anchorage-path', help='Anchorage file pattern (glob)')
    parser.add_argument('--output',
                                            dest='output',
                                            help='Output file to write results to.')

    parser.add_argument('--input-table', default='pipeline_classify_p_p429_resampling_2',
                                            help='Input table to pull data from')

    parser.add_argument('--start-date', required=True, help="First date to look for entry/exit events.")

    parser.add_argument('--end-date', required=True, help="Last date (inclusive) to look for entry/exit events.")

    parser.add_argument('--start-window', help="date to start tracking events to warm up vessel state")


    parser.add_argument('--shard-output', type=bool, default=False, help="Whether to shard output or dump as single file")


    parser.add_argument('--fishing-mmsi-list',
                         dest='fishing_mmsi_list',
                         default='../treniformis/treniformis/_assets/GFW/FISHING_MMSI/KNOWN_LIKELY_AND_SUSPECTED/ANY_YEAR.txt',
                         help='location of list of newline separated fishing mmsi')



    known_args, pipeline_args = parser.parse_known_args(argv)

    if known_args.output is None:
        known_args.output = 'gs://machine-learning-dev-ttl-30d/anchorages/{}/output/encounters'.format(known_args.name)

    cmn.add_pipeline_defaults(pipeline_args, known_args.name)

    with open(known_args.fishing_mmsi_list) as f:
        fishing_vessels = set([int(x.strip()) for x in f.readlines() if x.strip()])

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    cmn.check_that_pipeline_args_consumed(pipeline_options)
    pipeline_options.view_as(SetupOptions).save_main_session = True

    p = beam.Pipeline(options=pipeline_options)


    # TODO: Should go in config file or arguments >>>
    min_required_positions = 200 
    stationary_period_min_duration = datetime.timedelta(hours=12)
    stationary_period_max_distance = 0.5 # km
    min_unique_vessels_for_anchorage = 20
    blacklisted_mmsis = [0, 12345]
    anchorage_visit_max_distance = 3.0 # km

    assert anchorage_visit_max_distance + cmn.approx_visit_cell_size * cmn.VISIT_SAFETY_FACTOR < VISIT_BUFFER_KM

    anchorage_visit_min_duration = datetime.timedelta(minutes=180)
    # ^^^



    start_date = datetime.datetime.strptime(known_args.start_date, '%Y-%m-%d') 
    end_window = end_date = datetime.datetime.strptime(known_args.end_date, '%Y-%m-%d') 

    if known_args.start_window:
        start_window = datetime.datetime.strptime(known_args.start_window, '%Y-%m-%d')
    else:
        start_window = start_date - datetime.timedelta(days=1)


    start_date = datetime.datetime.strptime(known_args.start_date, '%Y-%m-%d') 
    end_window = end_date = datetime.datetime.strptime(known_args.end_date, '%Y-%m-%d') 



    query = ais_query.format(table=known_args.input_table, start=start_window, end=end_window)




    ais_input_data = p | "ReadAis" >> Source(query)

    location_records = ais_input_data | CreateVesselRecords(blacklisted_mmsis)

    tagged_paths = location_records | CreateTaggedPaths(min_required_positions)


    # tagged_paths = (p 
    #     | "ReadAis" >> Source(query)
    #     | CreateVesselRecords(blacklisted_mmsis)
    #     | CreateTaggedPaths(min_required_positions)
    #     )


    # def order_by_timestamp(items):
    #     md, records = items
    #     try:
    #         records = list(records)
    #     except:
    #         raise RuntimeError("could not convert", type(records), "to list")
    #     return md, sorted(records, key=lambda x: x.timestamp)

    # grouped_records = (location_records 
    #     | "GroupByMmsi" >> beam.GroupByKey()
    #     | "OrderByTimestamp" >> beam.Map(order_by_timestamp)
    #     )

    # deduped_records = cmn.filter_duplicate_timestamps(grouped_records, min_required_positions)

    # thinned_records =   ( deduped_records 
    #                     | "ThinPoints" >> beam.Map(lambda (md, vlrs): (md, list(cmn.thin_points(vlrs)))))

    # thinned_records = tagged_paths

    # tagged_records = ( thinned_records 
    #                  | "TagWithDestinationAndId" >> beam.Map(lambda (md, records): (md, cmn.tag_with_destination_and_id(records))))

    # tagged_paths = ( tagged_records
    #                 | "TagPathsWithS2ids" >> beam.Map(lambda (md, records): (md, tag_records_with_nbr_s2id_set(records))))


    port_input_data= (p | 'ReadPort' >> ReadFromText(known_args.anchorage_path))


    port_data = beam.pvalue.AsDict(port_input_data 
        | "JsonToPortsDict" >> beam.Map(classic_json.loads)
        | "CreatePorts" >> beam.Map(PseudoAnchorage_from_json)
        | "TagWithVisitS2id" >> beam.Map(lambda x: (s2sphere.CellId.from_token(x.s2id).parent(cmn.VISITS_S2_SCALE).to_token(), x)))


    events = (tagged_paths | beam.FlatMap(find_in_out_events, port_entry_dist=3.0, port_exit_dist=4.0, anchorage_map=port_data))

    num_shards = (0 if known_args.shard_output else 1)

    (events
        | "convertAVToJson" >> beam.Map(event_to_json)
        | "writeAnchoragesVisits" >> WriteToText(known_args.output, file_name_suffix='.json', num_shards=num_shards))


    result = p.run()
    result.wait_until_finish()

