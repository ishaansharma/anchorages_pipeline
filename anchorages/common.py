
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
from .nearest_port import port_finder, AnchorageFinder, BUFFER_KM as VISIT_BUFFER_KM

import apache_beam as beam
from apache_beam.io import ReadFromText
from apache_beam.io import WriteToText
from apache_beam.metrics import Metrics
from apache_beam.metrics.metric import MetricsFilter
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions


VesselMetadata = namedtuple('VesselMetadata', ['mmsi'])

def VesselMetadata_from_msg(msg):
    return VesselMetadata(msg['mmsi'])


def filter_by_latlon(msg, filters):
    if not is_location_message(msg):
        # Keep non-location messages for destination tagging
        return True
    # Keep any message that falls within a filter region
    lat = msg['lat']
    lon = msg['lon']
    for bounds in filters:
        if ((bounds['min_lat'] <= lat <= bounds['max_lat']) and 
            (bounds['min_lon'] <= lon <= bounds['max_lon'])):
                return True
    # This message is not within any filter region.
    return False


TaggedVesselLocationRecord = namedtuple('TaggedVesselLocationRecord',
            ['destination', 's2id', 'is_new_id', 'timestamp', 'location', 'distance_from_shore', 'speed', 'course'])

VesselInfoRecord = namedtuple('VesselInfoRecord',
            ['timestamp', 'destination'])


AnchorageVisit = namedtuple('AnchorageVisit',
            ['anchorage', 'arrival', 'departure'])


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



def is_location_message(msg):
    return (
                'lat' in msg and 
                'lon' in msg and
                'speed' in msg and
                'distance_from_shore' in msg and
                'course' in msg
        )


def is_not_bad_value(lr):
    return isinstance(lr, VesselInfoRecord) or (
    -90 <= lr.location.lat <= 90 and
    -180 <= lr.location.lon <= 180 and
    0 <= lr.distance_from_shore <= 20000 and
    0 <= lr.course < 360 
    )


def read_json_records(input, blacklisted_mmsis, latlon_filters):
    parsed =  (input 
        | "Parse" >> beam.Map(json.loads)
        )

    if latlon_filters is not None:
        parsed = (parsed 
            | "filterByLatLon" >> beam.Filter(filter_by_latlon, latlon_filters)
        )

    return (parsed 
        | "CreateLocationRecords" >> beam.FlatMap(Records_from_msg, blacklisted_mmsis)
        | "FilterOutBadValues" >> beam.Filter(lambda (md, lr): is_not_bad_value(lr))
        )

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


StationaryPeriod = namedtuple("StationaryPeriod", ['location', 'start_time', 'duration', 'mean_distance_from_shore', 
        'rms_drift_radius', 'destination', 's2id'])

LatLon = namedtuple("LatLon", ["lat", "lon"])

def LatLon_from_LatLng(latlng):
    return LatLon(latlng.lat(), latlng.lon())

ProcessedLocations = namedtuple("ProcessedLocations", ['locations', 'stationary_periods'])


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


#  TODO: defunctionify
def filter_and_process_vessel_records(input, stationary_period_min_duration, stationary_period_max_distance, prefix=''):
    return ( input 
            | prefix + "splitIntoStationaryNonstationaryPeriods" >> beam.Map( lambda (metadata, records):
                        (metadata, 
                         remove_stationary_periods(records, stationary_period_min_duration, stationary_period_max_distance)))
            )

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



bogus_destinations = set([''])

def AnchoragePts_from_cell_visits(value, dest_limit, fishing_vessel_set):
    s2id, (stationary_periods, active_points) = value

    n = 0
    total_lat = 0.0
    total_lon = 0.0
    fishing_vessels = set()
    vessels = set()
    total_distance_from_shore = 0.0
    total_squared_drift_radius = 0.0
    active_mmsi = set(md for (md, loc) in active_points)
    active_mmsi_count = len(active_mmsi)
    active_days = len(set([(md, loc.timestamp.date()) for (md, loc) in active_points]))
    stationary_days = 0
    stationary_fishing_days = 0

    for (md, sp) in stationary_periods:
        n += 1
        total_lat += sp.location.lat
        total_lon += sp.location.lon
        vessels.add(md)
        stationary_days += sp.duration.total_seconds() / (24.0 * 60.0 * 60.0)
        if md.mmsi in fishing_vessel_set:
            fishing_vessels.add(md)
            stationary_fishing_days += sp.duration.total_seconds() / (24.0 * 60.0 * 60.0)
        total_distance_from_shore += sp.mean_distance_from_shore
        total_squared_drift_radius += sp.rms_drift_radius ** 2
    all_destinations = normalized_valid_names(sp.destination for (md, sp) in stationary_periods)

    total_mmsi_count = len(vessels | active_mmsi)

    if n:
        neighbor_s2ids = tuple(s2sphere.CellId.from_token(s2id).get_all_neighbors(ANCHORAGES_S2_SCALE))
        loc = LatLon(total_lat / n, total_lon / n)
        port_name, port_distance = port_finder.find_nearest_port_and_distance(loc)

        return [AnchoragePoint(
                    mean_location = loc,
                    total_visits = n, 
                    vessels = frozenset(vessels),
                    fishing_vessels = frozenset(fishing_vessels),
                    mean_distance_from_shore = total_distance_from_shore / n,
                    rms_drift_radius =  math.sqrt(total_squared_drift_radius / n),    
                    top_destinations = tuple(Counter(all_destinations).most_common(dest_limit)),
                    s2id = s2id,
                    neighbor_s2ids = neighbor_s2ids,
                    active_mmsi = active_mmsi_count,
                    total_mmsi = total_mmsi_count,
                    stationary_mmsi_days = stationary_days,
                    stationary_fishing_mmsi_days = stationary_fishing_days,
                    active_mmsi_days = active_days,
                    port_name = port_name,
                    port_distance = port_distance
                    )]
    else:
        return []


# def AnchoragePt_from_cell_visits(value, dest_limit):
#     s2id, visits = value

#     n = 0
#     total_lat = 0.0
#     total_lon = 0.0
#     vessels = set()
#     total_distance_from_shore = 0.0
#     total_squared_drift_radius = 0.0

#     for (md, pl) in visits:
#         n += 1
#         total_lat += pl.location.lat
#         total_lon += pl.location.lon
#         vessels.add(md)
#         total_distance_from_shore += pl.mean_distance_from_shore
#         total_squared_drift_radius += pl.rms_drift_radius ** 2
#     all_destinations = normalized_valid_names(pl.destination for (md, pl) in visits)

#     return AnchoragePoint(
#                 mean_location = LatLon(total_lat / n, total_lon / n),
#                 total_visits = n, 
#                 vessels = frozenset(vessels),
#                 mean_distance_from_shore = total_distance_from_shore / n,
#                 rms_drift_radius =  math.sqrt(total_squared_drift_radius / n),    
#                 top_destinations = tuple(Counter(all_destinations).most_common(dest_limit)),
#                 s2id = s2id,
#                 active_mmsi = 0,
#                 total_mmsi = 0 
#                 )                  


def find_anchorage_points(input, min_unique_vessels_for_anchorage, fishing_vessels):
    """
    input is a Pipeline object that contains [(md, processed_locations)]

    """
    # (md, stationary_periods) => (md (stationary_locs, )) 
    stationary_periods_by_s2id = (input
        | "addStationaryCellIds" >> beam.FlatMap(lambda (md, processed_locations):
                [(sp.s2id, (md, sp)) for sp in processed_locations.stationary_periods])
        )

    active_points_by_s2id = (input
        | "addActiveCellIds" >> beam.FlatMap(lambda (md, processed_locations):
                [(loc.s2id, (md, loc)) for loc in processed_locations.locations])
        )

    return ((stationary_periods_by_s2id, active_points_by_s2id) 
        | "CogroupOnS2id" >> beam.CoGroupByKey()
        | "createAnchoragePoints" >> beam.FlatMap(AnchoragePts_from_cell_visits, dest_limit=10, fishing_vessel_set=fishing_vessels)
        | "removeAPointsWFewVessels" >> beam.Filter(lambda x: len(x.vessels) >= min_unique_vessels_for_anchorage)
        )




def anchorage_point_to_json(a_pt):
    return json.dumps({'lat' : a_pt.mean_location.lat, 'lon': a_pt.mean_location.lon,
        'total_visits' : a_pt.total_visits,
        'drift_radius' : a_pt.rms_drift_radius,
        'destinations': a_pt.top_destinations,
        'unique_stationary_mmsi' : len(a_pt.vessels),
        'unique_stationary_fishing_mmsi' : len(a_pt.fishing_vessels),
        'unique_active_mmsi' : a_pt.active_mmsi,
        'unique_total_mmsi' : a_pt.total_mmsi,
        'active_mmsi_days': a_pt.active_mmsi_days,
        'stationary_mmsi_days': a_pt.stationary_mmsi_days,
        'stationary_fishing_mmsi_days': a_pt.stationary_fishing_mmsi_days,
        'port_name': a_pt.port_name,
        'port_distance': a_pt.port_distance,
        's2id' : a_pt.s2id
        })

             

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


class GroupAll(beam.CombineFn):

    def create_accumulator(self):
        return []

    def add_input(self, accumulator, value):
        accumulator.append(value)
        return accumulator

    def merge_accumulators(self, accumulators):
        return list(it.chain(*accumulators))

    def extract_output(self, accumulator):
        return accumulator


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


def tag_apts_with_nbr_s2ids(apt):
    """Tag anchorage pt with it's own and nbr ids at VISITS_S2_SCALE
    """
    s2_cell_id = s2sphere.CellId.from_token(apt.s2id).parent(VISITS_S2_SCALE)
    s2ids = [s2_cell_id.to_token()]
    for cell_id in s2_cell_id.get_all_neighbors(VISITS_S2_SCALE):
        s2ids.append(cell_id.to_token())
    return [(x, apt) for x in s2ids]
    

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
                ],

    'custom': [
                'gs://p_p429_resampling_3/data-production/classify-pipeline/classify/{date:%Y-%m-%d}/*-of-*'
                ]
    }

preset_runs['small'] = preset_runs['2016'][-3:]
preset_runs['medium'] = preset_runs['2016'][-6:]
