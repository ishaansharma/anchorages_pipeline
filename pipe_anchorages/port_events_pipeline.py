from __future__ import absolute_import, print_function, division

import datetime
import logging

import apache_beam as beam
from apache_beam.runners import PipelineState

from . import common as cmn
from .transforms.source import QuerySource
from .transforms.create_tagged_anchorages import CreateTaggedAnchorages
from .transforms.create_in_out_events import CreateInOutEvents
from .transforms.sink import EventSink
from .options import PortEventsOptions


def create_queries(args):
    template = """
    SELECT mmsi, lat, lon, timestamp, destination, speed FROM   
      TABLE_DATE_RANGE([world-fishing-827:{table}.], 
                        TIMESTAMP('{start:%Y-%m-%d}'), TIMESTAMP('{end:%Y-%m-%d}')) 
    """
    start_date = datetime.datetime.strptime(args.start_date, '%Y-%m-%d') 
    start_window = start_date - datetime.timedelta(days=1)
    end_date= datetime.datetime.strptime(args.end_date, '%Y-%m-%d') 
    while start_window <= end_date:
        end_window = min(start_window + datetime.timedelta(days=999), end_date)
        query = template.format(table=args.input_table, start=start_window, end=end_window)
        if args.fast_test:
            query += 'LIMIT 100000'
        yield query
        start_window = end_window + datetime.timedelta(days=1)



anchorage_query = 'SELECT lat anchor_lat, lon anchor_lon, anchor_id, FINAL_NAME FROM [{}]'


def run(options):

    known_args = options.view_as(PortEventsOptions)

    p = beam.Pipeline(options=options)

    config = cmn.load_config(known_args.config)

    queries = create_queries(known_args)

    sources = [(p | "Read_{}".format(i) >> beam.io.Read(beam.io.gcp.bigquery.BigQuerySource(query=x)))
                        for (i, x) in enumerate(queries)]

    tagged_records = (sources
        | beam.Flatten()
        | cmn.CreateVesselRecords(config['blacklisted_mmsis'])
        | cmn.CreateTaggedRecords(config['min_required_positions'])
        )

    anchorages = (p
        | 'ReadAnchorages' >> QuerySource(anchorage_query.format(known_args.anchorage_table))
        | CreateTaggedAnchorages()
        )

    (tagged_records
        | CreateInOutEvents(anchorages=anchorages,
                            anchorage_entry_dist=config['anchorage_entry_distance_km'], 
                            anchorage_exit_dist=config['anchorage_exit_distance_km'], 
                            stopped_begin_speed=config['stopped_begin_speed_knots'],
                            stopped_end_speed=config['stopped_end_speed_knots'])
        | "writeInOutEvents" >> EventSink(table=known_args.output_table, write_disposition="WRITE_APPEND")
        )


    result = p.run()

    success_states = set([PipelineState.DONE, PipelineState.RUNNING, PipelineState.UNKNOWN])

    logging.info('returning with result.state=%s' % result.state)
    return 0 if result.state in success_states else 1


