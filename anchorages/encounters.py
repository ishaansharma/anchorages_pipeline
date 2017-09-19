from __future__ import absolute_import, print_function, division

import argparse
import logging
import re
import ujson as json
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


# Port arrivals and departures.
# First cut:
# 0.5 hours in port (or missing) == Arrival
# First point outside a port after being in Port --> Departure.
# Avoid multiple in-out event;


AnchoragePoint = namedtuple("AnchoragePoint", ['mean_location',
                                               'total_visits',
                                               'vessels',
                                               'fishing_vessels',
                                               'mean_distance_from_shore',
                                               'rms_drift_radius',
                                               'top_destinations',
                                               's2id',
                                               'neighbor_s2ids',
                                               'active_mmsi',
                                               'total_mmsi',
                                               'stationary_mmsi_days',
                                               'stationary_fishing_mmsi_days',
                                               'active_mmsi_days',
                                               'port_name',
                                               'port_distance'
                                               ])


ProcessedLocations = namedtuple("ProcessedLocations", ['locations', 'stationary_periods'])


def tag_with_destination_and_id(records):
    """filter out info messages and use them to tag subsequent records"""
    # TODO: think about is_new_id, we don't currently use it. Original thought was to count
    # the average number of vessels in a cell at one time. For that we probably want to swithc
    # to is_first, and is_last for the first and last points in the cell. Could be done by
    # modifying the last item in the cell as well.
    dest = ''
    new = []
    last_s2id = None
    for rcd in records:
        if isinstance(rcd, VesselInfoRecord):
            # TODO: normalize here rather than later. And cache normalization in dictionary
            dest = rcd.destination
        else:
            s2id = S2_cell_ID(rcd.location).to_token()
            is_new_id = (s2id == last_s2id)
            new.append(TaggedVesselLocationRecord(dest, s2id, is_new_id,
                                                  *rcd)) 
            last_s2id = is_new_id
    return new


class VesselLocationRecord(
    namedtuple("VesselLocationRecord",
              ['timestamp', 'location', 'distance_from_shore', 'speed', 'course'])):

    @classmethod
    def from_msg(cls, msg):
        latlon = LatLon(msg['lat'], msg['lon'])

        return cls(
            datetime.datetime.strptime(msg['timestamp'], '%Y-%m-%dT%H:%M:%S.%fZ'), 
            latlon, 
            msg['distance_from_shore'] / 1000.0,
            round(msg['speed'], 1),
            msg['course']            )

TaggedVesselLocationRecord = namedtuple('TaggedVesselLocationRecord',
            ['destination', 's2id', 'is_new_id', 'timestamp', 'location', 'distance_from_shore', 'speed', 'course'])

VesselInfoRecord = namedtuple('VesselInfoRecord',
            ['timestamp', 'destination'])

def dedup_by_timestamp(element):
    key, source = element
    seen = set()
    sink = []
    for x in source:
        if x.timestamp not in seen:
            sink.append(x)
            seen.add(x.timestamp)
    return (key, sink)

def filter_duplicate_timestamps(input, min_required_positions):
    return (input
           | "RemoveDupTStamps" >> beam.Map(dedup_by_timestamp)
           | "RemoveShortSeries" >> beam.Filter(lambda x: len(x[1]) >= min_required_positions)
           )



# {"stationary_fishing_mmsi_days":0,"s2id":"3be62991","port_name":["MUMBAI (BOMBAY)","IN",18.966667,72.866667],"unique_total_mmsi":196,"lat":18.7687215718,"unique_stationary_fishing_mmsi":0,"destinations":[["JNPT",11],["NHAVA SHEVA",6],["MUMBAI",4],["INNSA",2],["JNPT INDIA",2],["JNPT MUMBAI",2],["KARACHI",1],["AE KLF",1],["NHAVA SHEVA ARMGUARD",1],["NHAVASHEVA",1]],"port_distance":35.6361580048,"active_mmsi_days":251,"unique_stationary_mmsi":43,"lon":72.6003083819,"unique_active_mmsi":195,"drift_radius":0.1170197889,"stationary_mmsi_days":92.3241898148,"total_visits":44}

VesselInfoRecord = namedtuple('VesselInfoRecord',
            ['timestamp', 'destination'])


VesselMetadata = namedtuple('VesselMetadata', ['mmsi'])

def VesselMetadata_from_msg(msg):
    return VesselMetadata(msg['mmsi'])


PseudoAnchorage = namedtuple("PseudoAnchorage", ['mean_location', "s2id", "port_name"])

def PseudoAnchorage_from_json(obj):
    return PseudoAnchorage(LatLon(obj['lat'], obj['lon']), obj['s2id'], Port._make(obj['port_name']))


LatLon = namedtuple("LatLon", ["lat", "lon"])

def LatLon_from_LatLng(latlng):
    return LatLon(latlng.lat(), latlng.lon())


def S2_cell_ID(loc):
    ll = s2sphere.LatLng.from_degrees(loc.lat, loc.lon)
    return s2sphere.CellId.from_lat_lng(ll).parent(ANCHORAGES_S2_SCALE)

def mean(iterable):
    n = 0
    total = 0.0
    for x in iterable:
        total += x
        n += 1
    return (total / n) if n else 0

def LatLon_mean(seq):
    seq = list(seq)
    return LatLon(mean(x.lat for x in seq), mean(x.lon for x in seq))
         

def datetime_to_text(dt):
    return datetime.datetime.strftime(dt, '%Y-%m-%dT%H:%M:%SZ')

def single_anchorage_visit_to_json(visit):
    anchorage, stationary_period  = visit
    return {'port_name' : anchorage.name,
            'port_country' : anchorage.country,
            'port_lat' : anchorage.lat,
            'port_lon' : anchorage.lon,
            'port_s2id' : anchorage.anchorage_point.s2id, 
            'arrival': datetime_to_text(stationary_period.start_time),
            'duration_hours': stationary_period.duration.total_seconds() / (60.0 * 60.0),
            'mean_lat': stationary_period.location.lat,
            'mean_lon': stationary_period.location.lon
            }

def tagged_anchorage_visits_to_json(tagged_visits):
    metadata, visits = tagged_visits
    return json.dumps({'mmsi' : metadata.mmsi, 
        'visits': [single_anchorage_visit_to_json(x) for x in visits]})


def find_visits(s2id, md_sp_tuples, anchorage_points, max_distance):
    anchorage_finder = AnchorageFinder(anchorage_points)
    visits = []
    for md, sp in md_sp_tuples:
        anch = anchorage_finder.is_within(max_distance, sp, s2id=s2id)
        if anch is not None:
            visits.append((md, (anch, sp)))
    return visits



def check_that_pipeline_args_consumed(pipeline):
    options = pipeline.get_all_options(drop_default=True)

    # Some options get translated on the way in (should be a better way to do this...)
    translations = {'--worker_machine_type' : '--machine_type'}
    flags = [translations.get(x, x) for x in pipeline._flags]

    dash_flags = [x for x in flags if x.startswith('-') and x.replace('-', '') not in options]
    if dash_flags:
        print(options)
        print(dash_flags)
        raise ValueError('illegal options specified:\n    {}'.format('\n    '.join(dash_flags)))


def add_pipeline_defaults(pipeline_args, name):

    defaults = {
        '--project' : 'world-fishing-827',
        '--staging_location' : 'gs://machine-learning-dev-ttl-30d/anchorages/{}/output/staging'.format(name),
        '--temp_location' : 'gs://machine-learning-dev-ttl-30d/anchorages/temp',
        '--setup_file' : './setup.py',
        '--runner': 'DataflowRunner',
        '--max_num_workers' : '200',
        '--job_name': name,
    }

    for name, value in defaults.items():
        if name not in pipeline_args:
            pipeline_args.extend((name, value))


def tag_apts_with_nbr_s2ids(apt):
    """Tag anchorage pt with it's own and nbr ids at VISITS_S2_SCALE
    """
    s2_cell_id = s2sphere.CellId.from_token(apt.s2id).parent(VISITS_S2_SCALE)
    s2ids = [s2_cell_id.to_token()]
    for cell_id in s2_cell_id.get_all_neighbors(VISITS_S2_SCALE):
        s2ids.append(cell_id.to_token())
    return [(x, apt) for x in s2ids]


# # Around (1 km)^2
# ANCHORAGES_S2_SCALE = 13
# Around (0.5 km)^2
ANCHORAGES_S2_SCALE = 14
# Around (8 km)^2
VISITS_S2_SCALE = 10
#
# TODO: revisit
approx_visit_cell_size = 2.0 ** (13 - VISITS_S2_SCALE) 
VISIT_SAFETY_FACTOR = 2.0 # Extra margin factor to ensure VISIT_BUFFER_KM is large enough

def is_location_message(msg):
    return (
                'lat' in msg and 
                'lon' in msg and
                'speed' in msg and
                'distance_from_shore' in msg and
                'course' in msg
        )

FIVE_MINUTES = datetime.timedelta(minutes=5)

def thin_points(records):
    # TODO: consider instead putting on five minute intervals? 
    if not records:
        return
    last = records[0]
    yield last
    for vlr in records[1:]:
        if (vlr.timestamp - last.timestamp) >= FIVE_MINUTES:
            last = vlr
            yield vlr


def Records_from_msg(msg, blacklisted_mmsis):

    mmsi = msg.get('mmsi')
    if not isinstance(mmsi, int) or (mmsi in blacklisted_mmsis):
        return []

    metadata = VesselMetadata_from_msg(msg)

    if is_location_message(msg):
        return [(metadata, VesselLocationRecord.from_msg(msg))]
    elif msg.get('destination') not in set(['', None]):
        return [(metadata, VesselInfoRecord(
            datetime.datetime.strptime(msg['timestamp'], '%Y-%m-%dT%H:%M:%S.%fZ'),
            msg['destination']
            ))]
    else:
        return []


preset_runs = {

    'tiny' : ['gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-01-01/001-of-*'],

    '2016' : [
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-01-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-02-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-03-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-04-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-05-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-06-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-07-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-08-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-09-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-10-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-11-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-12-*/*-of-*'
                ],

    'all_years': [
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2012-*-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2013-*-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2014-*-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2015-*-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2016-*-*/*-of-*',
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/2017-*-*/*-of-*'
                ]
    }

preset_runs['small'] = preset_runs['2016'][:3]
preset_runs['medium'] = preset_runs['2016'][-6:]


def is_not_bad_value(lr):
    return isinstance(lr, VesselInfoRecord) or (
    -90 <= lr.location.lat <= 90 and
    -180 <= lr.location.lon <= 180 and
    0 <= lr.distance_from_shore <= 20000 and
    0 <= lr.course < 360 
    )


#  TODO: defunctionify
def filter_and_process_vessel_records(input, stationary_period_min_duration, stationary_period_max_distance, prefix=''):
    return ( input 
            | prefix + "splitIntoStationaryNonstationaryPeriods" >> beam.Map( lambda (metadata, records):
                        (metadata, 
                         remove_stationary_periods(records, stationary_period_min_duration, stationary_period_max_distance)))
            )


StationaryPeriod = namedtuple("StationaryPeriod", ['location', 'start_time', 'duration', 'mean_distance_from_shore', 
        'rms_drift_radius', 'destination', 's2id'])


def remove_stationary_periods(records, stationary_period_min_duration, stationary_period_max_distance):
    # Remove long stationary periods from the record: anything over the threshold
    # time will be reduced to just the start and end points of the period.

    without_stationary_periods = []
    stationary_periods = []
    current_period = []

    for vr in records:
        if current_period:
            first_vr = current_period[0]
            if distance(vr.location, first_vr.location) > stationary_period_max_distance:
                if current_period[-1].timestamp - first_vr.timestamp > stationary_period_min_duration:
                    without_stationary_periods.append(first_vr)
                    if current_period[-1] != first_vr:
                        without_stationary_periods.append(current_period[-1])
                    num_points = len(current_period)
                    duration = current_period[-1].timestamp - first_vr.timestamp
                    mean_lat = sum(x.location.lat for x in current_period) / num_points
                    mean_lon = sum(x.location.lon for x in current_period) / num_points
                    mean_location = LatLon(mean_lat, mean_lon)
                    mean_distance_from_shore = sum(x.distance_from_shore for x in current_period) / num_points
                    rms_drift_radius = math.sqrt(sum(distance(x.location, mean_location)**2 for x in current_period) / num_points)
                    stationary_periods.append(StationaryPeriod(mean_location, 
                                                               first_vr.timestamp,
                                                               duration, 
                                                               mean_distance_from_shore, rms_drift_radius,
                                                               first_vr.destination,
                                                               s2id=S2_cell_ID(mean_location).to_token()))
                else:
                    without_stationary_periods.extend(current_period)
                current_period = []
        current_period.append(vr)
    without_stationary_periods.extend(current_period)

    return ProcessedLocations(without_stationary_periods, stationary_periods) 


def run(argv=None):
    """Main entry point; defines and runs the wordcount pipeline.
    """

    parser = argparse.ArgumentParser()

    parser.add_argument('--name', required=True, help='Name to prefix output and job name if not otherwise specified')

    parser.add_argument('--port-patterns', help='Input file patterns (comma separated) for anchorages output to process (glob)')
    parser.add_argument('--output',
                                            dest='output',
                                            help='Output file to write results to.')

    parser.add_argument('--input-patterns', default='all_years',
                                            help='Input file to patterns (comma separated) to process (glob)')

    parser.add_argument('--start-date', required=True, help="First date to look for encounters starting.")

    parser.add_argument('--end-date', required=True, help="Last date (inclusive) to look for encounters starting.")

    parser.add_argument('--end-window', help="last date (inclusive) to look for encounters ending")



    parser.add_argument('--fishing-mmsi-list',
                         dest='fishing_mmsi_list',
                         default='../treniformis/treniformis/_assets/GFW/FISHING_MMSI/KNOWN_LIKELY_AND_SUSPECTED/ANY_YEAR.txt',
                         help='location of list of newline separated fishing mmsi')



    known_args, pipeline_args = parser.parse_known_args(argv)

    if known_args.output is None:
        known_args.output = 'gs://machine-learning-dev-ttl-30d/anchorages/{}/output/encounters'.format(known_args.name)

    add_pipeline_defaults(pipeline_args, known_args.name)

    with open(known_args.fishing_mmsi_list) as f:
        fishing_vessels = set([int(x.strip()) for x in f.readlines() if x.strip()])

    # We use the save_main_session option because one or more DoFn's in this
    # workflow rely on global context (e.g., a module imported at module level).
    pipeline_options = PipelineOptions(pipeline_args)
    check_that_pipeline_args_consumed(pipeline_options)
    pipeline_options.view_as(SetupOptions).save_main_session = True

    p = beam.Pipeline(options=pipeline_options)


    # TODO: Should go in config file or arguments >>>
    min_required_positions = 200 
    stationary_period_min_duration = datetime.timedelta(hours=12)
    stationary_period_max_distance = 0.5 # km
    min_unique_vessels_for_anchorage = 20
    blacklisted_mmsis = [0, 12345]
    anchorage_visit_max_distance = 3.0 # km

    assert anchorage_visit_max_distance + approx_visit_cell_size * VISIT_SAFETY_FACTOR < VISIT_BUFFER_KM

    anchorage_visit_min_duration = datetime.timedelta(minutes=180)
    # ^^^

    if known_args.input_patterns in preset_runs:
        input_patterns = preset_runs[known_args.input_patterns]
    else:
        input_patterns = [x.strip() for x in known_args.input_patterns.split(',')]


    start_window = start_date = datetime.datetime.strptime(known_args.start_date, '%Y-%m-%d') 
    end_date = datetime.datetime.strptime(known_args.end_date, '%Y-%m-%d') 

    if known_args.end_window:
        end_window = datetime.datetime.strptime(known_args.end_window, '%Y-%m-%d')
    else:
        end_window = end_date + datetime.timedelta(days=1)


    ais_input_data_streams = [(p | 'ReadAis_{}'.format(i) >> ReadFromText(x)) for (i, x) in  enumerate(input_patterns)]

    ais_input_data = ais_input_data_streams | beam.Flatten() 

    location_records = (ais_input_data 
        | "ParseAis" >> beam.Map(json.loads)
        | "CreateLocationRecords" >> beam.FlatMap(Records_from_msg, blacklisted_mmsis)
        | "FilterByDateWindow" >> beam.Filter(lambda (md, lr): start_window <= lr.timestamp <= end_window)
        | "FilterOutBadValues" >> beam.Filter(lambda (md, lr): is_not_bad_value(lr))
        )


    grouped_records = (location_records 
        | "GroupByMmsi" >> beam.GroupByKey()
        | "OrderByTimestamp" >> beam.Map(lambda (md, records): (md, sorted(records, key=lambda x: x.timestamp)))
        )

    deduped_records = filter_duplicate_timestamps(grouped_records, min_required_positions)

    thinned_records =   ( deduped_records 
                        | "ThinPoints" >> beam.Map(lambda (md, vlrs): (md, list(thin_points(vlrs)))))

    tagged_records = ( thinned_records 
                     | "TagWithDestinationAndId" >> beam.Map(lambda (md, records): (md, tag_with_destination_and_id(records))))



    port_patterns = [x.strip() for x in known_args.port_patterns.split(',')]

    print("YYY", len(port_patterns))
    print(port_patterns)


    port_input_data_streams = [(p | 'ReadPort_{}'.format(i) >> ReadFromText(x)) for (i, x) in  enumerate(port_patterns)]


    if len(port_patterns) > 1:
        port_data = port_input_data_streams | "FlattenPorts" >> beam.Flatten()
    else:
        port_data = port_input_data_streams[0]


    anchorage_points = (port_data
        | "ParseAnchorages" >> beam.Map(json.loads)
        | "CreateAnchoragePoints" >> beam.Map(PseudoAnchorage_from_json)
        )


    tagged_anchorage_points = ( anchorage_points 
                              | "tagAnchoragePointsWithNbrS2ids" >> beam.FlatMap(tag_apts_with_nbr_s2ids)
                              )

    # TODO: this is a very broad stationary distance.... is that what we want. Might be, but think about it.
    visit_records = filter_and_process_vessel_records(tagged_records, anchorage_visit_min_duration, anchorage_visit_max_distance,
                    prefix="anchorages")

    tagged_for_visits_records = ( visit_records  
                       | "TagSPWithS2id" >> beam.FlatMap(lambda (md, processed_locations): 
                                                [(s2sphere.CellId.from_token(sp.s2id).parent(VISITS_S2_SCALE).to_token(), 
                                                    (md, sp)) for sp in processed_locations.stationary_periods])
                       )


    anchorage_visits = ( (tagged_for_visits_records, tagged_anchorage_points)
                       | "CoGroupByS2id" >> beam.CoGroupByKey()
                       | "FindVisits"  >> beam.FlatMap(lambda (s2id, (md_sp_tuples, apts)): 
                                        (find_visits(s2id, md_sp_tuples, apts, anchorage_visit_max_distance)))  
                       | "FilterByDate" >> beam.Filter(lambda (md, (anch, sp)): start_date <= sp.start_time <= end_date)
                       | "GroupVisitsByMd" >> beam.GroupByKey()
                       )

    (anchorage_visits 
        | "convertAVToJson" >> beam.Map(tagged_anchorage_visits_to_json)
        | "writeAnchoragesVisits" >> WriteToText(known_args.output, file_name_suffix='.json')
    )


    result = p.run()
    result.wait_until_finish()

