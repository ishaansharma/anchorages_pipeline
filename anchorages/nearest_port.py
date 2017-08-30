from __future__ import print_function, division
import csv
import os
from collections import namedtuple
from .distance import distance, EARTH_RADIUS

Port = namedtuple("Port", ["name", "country", "lat", "lon"])

this_dir = os.path.dirname(__file__)


class PortFinder(object):

    def __init__(self, anchorage_path="ports.csv", buffer_km=100.0):
        self.buffer_km = buffer_km
        self.ports_near = {}
        self.ports = []
        with open(os.path.join(this_dir, anchorage_path)) as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.ports.append(Port(name=row['port_name'],
                                       country=row['country'],
                                       lat=float(row['latitude']),
                                       lon=float(row['longitude'])))

    def __call__(self, loc):
        return self.find_nearest_port_and_distance(loc)[0]


    def find_nearest_port_and_distance(self, loc):
        min_p = self.ports[0]
        min_dist = distance(min_p, loc)
        for p in self.ports[1:]:
            dist = distance(p, loc)
            if dist > min_dist:
                continue
            min_p = p
            min_dist = dist
        return min_p, min_dist


    def port_within(self, distance, location_record):
        loc = location_record.locations
        if location_record.s2id not in self.ports_near:
            ports = []
            for p in self.ports:
                dist = distance(p, loc)
                if dist <= self.buffer_km:
                    ports.append(p)
            self.ports_near = ports
        candidates = sorted([(distance(p, loc), p) for p in self.ports_near[location_record.s2id]])
        if candidates:
            dist, port = candidates[0]
            return p
        else:
            return None


port_finder = PortFinder()


