from __future__ import absolute_import, print_function, division

import datetime
import logging

import apache_beam as beam
from apache_beam import Filter
from apache_beam.options.pipeline_options import GoogleCloudOptions
from apache_beam.runners import PipelineState

from google.cloud import bigquery
from google.cloud.exceptions import NotFound

from . import common as cmn
from .transforms.source import QuerySource
from .transforms.create_tagged_anchorages import CreateTaggedAnchorages
from .transforms.create_in_out_events import CreateInOutEvents
from .transforms.sink import EventSink, EventStateSink
from .options.port_events_options import PortEventsOptions
from .schema.port_event import build_event_state_schema 


def create_queries(args):
    template = """
    SELECT seg_id AS ident, ssvid, lat, lon, timestamp, speed -- use seg_id TODO
    FROM `{table}*`
    WHERE _table_suffix BETWEEN '{start:%Y%m%d}' AND '{end:%Y%m%d}' 

    -- AND vessel_id = '0d59d9f05-5189-96fb-f1d9-28d0294e4924' -- REMOVE
    """
    start_date = datetime.datetime.strptime(args.start_date, '%Y-%m-%d')
    start_window = start_date
    end_date= datetime.datetime.strptime(args.end_date, '%Y-%m-%d') 
    shift = 1000
    while start_window <= end_date:
        end_window = min(start_window + datetime.timedelta(days=shift), end_date)
        query = template.format(table=args.input_table, start=start_window, end=end_window)
        if args.fast_test:
            query += 'LIMIT 100000'
        yield query
        start_window = end_window + datetime.timedelta(days=1)


def create_state_query(args):
    template = """
    SELECT *
    FROM `{table}*`
    WHERE _table_suffix ='{date:%Y%m%d}'' 
    """
    start_date = datetime.datetime.strptime(args.start_date, '%Y-%m-%d') 
    date = start_date - datetime.timedelta(days=1)
    return template.format(table=args.state_table, date=date)


anchorage_query = 'SELECT lat as anchor_lat, lon as anchor_lon, s2id as anchor_id, label FROM `{}`'


def run(options):

    known_args = options.view_as(PortEventsOptions)
    cloud_options = options.view_as(GoogleCloudOptions)

    start_date = datetime.datetime.strptime(known_args.start_date, '%Y-%m-%d')
    end_date = datetime.datetime.strptime(known_args.end_date, '%Y-%m-%d')

    p = beam.Pipeline(options=options)

    config = cmn.load_config(known_args.config)

    queries = create_queries(known_args)

    sources = [(p | "Read_{}".format(i) >> 
                        beam.io.Read(beam.io.gcp.bigquery.BigQuerySource(query=x, use_standard_sql=True)))
                        for (i, x) in enumerate(queries)]

    tagged_records = (sources
        | beam.Flatten()
        | cmn.CreateVesselRecords(destination=None)
        | cmn.CreateTaggedRecords(min_required_positions=1, thin=False)
        )


    # TODO: this assumes track id, need to revert to seg_id try that version
    # TODO: then think about stitching together across segments.


    prev_date = start_date - datetime.timedelta(days=1)
    state_table = f'{known_args.state_table}{prev_date:%Y%m%d}'

    # TODO: make into separate function
    client = bigquery.Client(project=cloud_options.project)
    try:
        client.get_table(state_table) 
        state_table_exists = True
    except NotFound:
        state_table_exists = False

    if state_table_exists:
        initial_states = (p
            | beam.io.Read(beam.io.gcp.bigquery.BigQuerySource(table=state_table, validate=True))
            | beam.Map(lambda x : (x['seg_id'], x))
        )
    else:
        logging.warning('No state table found, using empty table')
        initial_states = (p | beam.Create([]))

    tagged_records = {'records' : tagged_records, 'state' : initial_states} | beam.CoGroupByKey()

    anchorages = (p
        # TODO: why not just use BigQuerySource?
        | 'ReadAnchorages' >> QuerySource(anchorage_query.format(known_args.anchorage_table),
                                           use_standard_sql=True)
        | CreateTaggedAnchorages()
        )

    tagged_records, states = (tagged_records
        | CreateInOutEvents(anchorages=anchorages,
                            anchorage_entry_dist=config['anchorage_entry_distance_km'], 
                            anchorage_exit_dist=config['anchorage_exit_distance_km'], 
                            stopped_begin_speed=config['stopped_begin_speed_knots'],
                            stopped_end_speed=config['stopped_end_speed_knots'],
                            min_gap_minutes=config['minimum_port_gap_duration_minutes'],
                            start_date=start_date, 
                            end_date=end_date)
        )

    (tagged_records 
        | "writeInOutEvents" >> EventSink(table=known_args.output_table, 
                                          temp_location=cloud_options.temp_location,
                                          project=cloud_options.project)
        )

    states | 'WriteState' >> EventStateSink(known_args.state_table, 
                                           temp_location=cloud_options.temp_location,
                                            project=cloud_options.project)


    result = p.run()

    success_states = set([PipelineState.DONE, PipelineState.RUNNING, PipelineState.UNKNOWN, PipelineState.PENDING])

    logging.info('returning with result.state=%s' % result.state)
    return 0 if result.state in success_states else 1


