from __future__ import unicode_literals

import bisect
import datetime
from collections import defaultdict
import json
from moto.core import BaseBackend, BaseModel
from moto.ec2 import ec2_backends
from .exceptions import BadSegmentException, AWSError


class TelemetryRecords(BaseModel):
    def __init__(self, instance_id, hostname, resource_arn, records):
        self.instance_id = instance_id
        self.hostname = hostname
        self.resource_arn = resource_arn
        self.records = records

    @classmethod
    def from_json(cls, json):
        instance_id = json.get('EC2InstanceId', None)
        hostname = json.get('Hostname')
        resource_arn = json.get('ResourceARN')
        telemetry_records = json['TelemetryRecords']

        return cls(instance_id, hostname, resource_arn, telemetry_records)


# https://docs.aws.amazon.com/xray/latest/devguide/xray-api-segmentdocuments.html
class TraceSegment(BaseModel):
    def __init__(self, name, segment_id, trace_id, start_time, end_time=None, in_progress=False, service=None, user=None,
                 origin=None, parent_id=None, http=None, aws=None, metadata=None, annotations=None, subsegments=None, **kwargs):
        self.name = name
        self.id = segment_id
        self.trace_id = trace_id
        self._trace_version = None
        self._original_request_start_time = None
        self._trace_identifier = None
        self.start_time = start_time
        self._start_date = None
        self.end_time = end_time
        self._end_date = None
        self.in_progress = in_progress
        self.service = service
        self.user = user
        self.origin = origin
        self.parent_id = parent_id
        self.http = http
        self.aws = aws
        self.metadata = metadata
        self.annotations = annotations
        self.subsegments = subsegments
        self.misc = kwargs

    def __lt__(self, other):
        return self.start_date < other.start_date

    @property
    def trace_version(self):
        if self._trace_version is None:
            self._trace_version = int(self.trace_id.split('-', 1)[0])
        return self._trace_version

    @property
    def request_start_date(self):
        if self._original_request_start_time is None:
            start_time = int(self.trace_id.split('-')[1], 16)
            self._original_request_start_time = datetime.datetime.fromtimestamp(start_time)
        return self._original_request_start_time

    @property
    def start_date(self):
        if self._start_date is None:
            self._start_date = datetime.datetime.fromtimestamp(self.start_time)
        return self._start_date

    @property
    def end_date(self):
        if self._end_date is None:
            self._end_date = datetime.datetime.fromtimestamp(self.end_time)
        return self._end_date

    @classmethod
    def from_dict(cls, data):
        # Check manditory args
        if 'id' not in data:
            raise BadSegmentException(code='MissingParam', message='Missing segment ID')
        seg_id = data['id']
        data['segment_id'] = seg_id  # Just adding this key for future convenience

        for arg in ('name', 'trace_id', 'start_time'):
            if arg not in data:
                raise BadSegmentException(seg_id=seg_id, code='MissingParam', message='Missing segment ID')

        if 'end_time' not in data and 'in_progress' not in data:
            raise BadSegmentException(seg_id=seg_id, code='MissingParam', message='Missing end_time or in_progress')
        if 'end_time' not in data and data['in_progress'] == 'false':
            raise BadSegmentException(seg_id=seg_id, code='MissingParam', message='Missing end_time')

        return cls(**data)


class SegmentCollection(object):
    def __init__(self):
        self._segments = defaultdict(self._new_trace_item)

    @staticmethod
    def _new_trace_item():
        return {
            'start_date': datetime.datetime(1970, 1, 1),
            'end_date': datetime.datetime(1970, 1, 1),
            'finished': False,
            'segments': []
        }

    def put_segment(self, segment):
        # insert into a sorted list
        bisect.insort_left(self._segments[segment.trace_id]['segments'], segment)

        # Get the last segment (takes into account incorrect ordering)
        # and if its the last one, mark trace as complete
        if self._segments[segment.trace_id]['segments'][-1].end_time is not None:
            self._segments[segment.trace_id]['finished'] = True

            start_time = self._segments[segment.trace_id]['segments'][0].start_date
            end_time = self._segments[segment.trace_id]['segments'][-1].end_date
            self._segments[segment.trace_id]['start_date'] = start_time
            self._segments[segment.trace_id]['end_date'] = end_time

            # Todo consolidate trace segments into a trace.
            # not enough working knowledge of xray to do this

    def summary(self, start_time, end_time, filter_expression=None, sampling=False):
        # This beast https://docs.aws.amazon.com/xray/latest/api/API_GetTraceSummaries.html#API_GetTraceSummaries_ResponseSyntax
        if filter_expression is not None:
            raise AWSError('Not implemented yet - moto', code='InternalFailure', status=500)

        summaries = []

        for tid, trace in self._segments.items():
            if trace['finished'] and start_time < trace['start_date'] and trace['end_date'] < end_time:
                duration = int((trace['end_date'] - trace['start_date']).total_seconds())
                # this stuff is mostly guesses, refer to TODO above
                has_error = any(['error' in seg.misc for seg in trace['segments']])
                has_fault = any(['fault' in seg.misc for seg in trace['segments']])
                has_throttle = any(['throttle' in seg.misc for seg in trace['segments']])

                # Apparently all of these options are optional
                summary_part = {
                    'Annotations': {},  # Not implemented yet
                    'Duration': duration,
                    'HasError': has_error,
                    'HasFault': has_fault,
                    'HasThrottle': has_throttle,
                    'Http': {},  # Not implemented yet
                    'Id': tid,
                    'IsParital': False,  # needs lots more work to work on partials
                    'ResponseTime': 1,  # definitely 1ms resposnetime
                    'ServiceIds': [],  # Not implemented yet
                    'Users': {}  # Not implemented yet
                }
                summaries.append(summary_part)

        result = {
            "ApproximateTime": int((datetime.datetime.now() - datetime.datetime(1970, 1, 1)).total_seconds()),
            "TracesProcessedCount": len(summaries),
            "TraceSummaries": summaries
        }

        return result


class XRayBackend(BaseBackend):

    def __init__(self):
        self._telemetry_records = []
        self._segment_collection = SegmentCollection()

    def add_telemetry_records(self, json):
        self._telemetry_records.append(
            TelemetryRecords.from_json(json)
        )

    def process_segment(self, doc):
        try:
            data = json.loads(doc)
        except ValueError:
            raise BadSegmentException(code='JSONFormatError', message='Bad JSON data')

        try:
            # Get Segment Object
            segment = TraceSegment.from_dict(data)
        except ValueError:
            raise BadSegmentException(code='JSONFormatError', message='Bad JSON data')

        try:
            # Store Segment Object
            self._segment_collection.put_segment(segment)
        except Exception as err:
            raise BadSegmentException(seg_id=segment.id, code='InternalFailure', message=str(err))

    def get_trace_summary(self, start_time, end_time, filter_expression, summaries):
        return self._segment_collection.summary(start_time, end_time, filter_expression, summaries)


xray_backends = {}
for region, ec2_backend in ec2_backends.items():
    xray_backends[region] = XRayBackend()
