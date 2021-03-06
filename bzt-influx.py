# Author: Jarrod Plant
# Taurus extension for reporting results into Influx.
# This extension does not include Influx. You must manage the installation separately.
# Data format closely matches (no guarantees) the data format from Jmeter

import copy
import logging
import os
import platform
import sys
import time
import traceback
import json
from abc import abstractmethod
from collections import defaultdict, OrderedDict, Counter, namedtuple
from ssl import SSLError
import requests
import yaml
from requests.exceptions import ReadTimeout, ConnectionError, ConnectTimeout
from urwid import Pile, Text
from bzt import TaurusInternalException, TaurusConfigError, TaurusException, TaurusNetworkError, NormalShutdown
from bzt.engine import Reporter, Provisioning, ScenarioExecutor, Configuration, Service
from bzt.engine import Singletone, SETTINGS
from bzt.modules.aggregator import DataPoint, KPISet, ConsolidatingAggregator, ResultsProvider, AggregatorListener
from bzt.modules.monitoring import Monitoring, MonitoringListener, LocalClient
from bzt.modules.services import Unpacker
from bzt.modules.selenium import SeleniumExecutor
from bzt.requests_model import has_variable_pattern
from bzt.six import BytesIO, iteritems, HTTPError, r_input, URLError, b, string_types, text_type
from bzt.utils import BetterDict
from bzt.utils import to_json, dehumanize_time

class InfluxUploader(Reporter, AggregatorListener, MonitoringListener, Singletone):
    """
    Reporter class
    """

    def __init__(self):
        super(InfluxUploader, self).__init__()
        self.kpi_buffer = []
        self.send_interval = 10
        self._last_status_check = time.time()
        self.send_data = True
        self.upload_artifacts = True
        self.send_monitoring = True
        self.monitoring_buffer = None
        self.last_dispatch = 0
        self.influx_url = "None"
        self.influx_application = "None"
        self.influx_measurement = "None"
        self.first_ts = sys.maxsize
        self.last_ts = 0
        self.report_name = None
        self._dpoint_serializer = InfluxDatapointSerializer(self)
        self.resend_timeout = 2.0

    def prepare(self):
        super(InfluxUploader, self).prepare()
        self.send_interval = dehumanize_time(self.settings.get("send-interval", self.send_interval))
        if isinstance(self.engine.aggregator, ResultsProvider):
            self.engine.aggregator.add_listener(self)
        self.influx_application=self.parameters.get("application")
        self.influx_measurement=self.parameters.get("measurement")
        self.influx_url=self.parameters.get("influx-url")
        if self.parameters.get("resend-timeout"):
            self.resend_timeout=float(self.parameters.get("resend-timeout"))


    def startup(self):
        """
        Logs test start time in influx
        """
        super(InfluxUploader, self).startup()
        self.__send_kpi_data("events,application="+self.influx_application + ",title=ApacheJMeter text=\"TestTitle started\" " +  str(int(round(time.time() * 1000))))
      
    def shutdown(self):
        """
        Logs test end time in influx
        """
        self.__send_kpi_data("events,application="+self.influx_application + ",title=ApacheJMeter text=\"TestTitle ended\" " +  str(int(round(time.time() * 1000))))

    def post_process(self):
        """
        Upload results if possible
        """
        self.log.info("KPI bulk buffer len in post-proc: %s", len(self.kpi_buffer))
        try:
            self.log.info("Influx: Sending remaining KPI data to server...")
            if self.send_data:
                self.__send_data(self.kpi_buffer)
                self.kpi_buffer = []
        except Exception as e:
            self.log.info("Error in post process")
            self.log.debug(str(e))

    def check(self):
        """
        Send data if any in buffer
        """
        self.log.debug("Influx: KPI bulk buffer len: %s", len(self.kpi_buffer))
        if self.last_dispatch < (time.time() - self.send_interval):
            self.last_dispatch = time.time()
            if self.send_data and len(self.kpi_buffer):
                self.__send_data(self.kpi_buffer)
                self.kpi_buffer = []

        return False


    def __send_data(self, data):
        """
        :type data: list[bzt.modules.aggregator.DataPoint]
        """
      
        serialized_list = self._dpoint_serializer.get_kpi_body(data)
        self.influx_application
        requestBody = ""
        for item in serialized_list: 
            requestBody = requestBody + self.influx_measurement +  ",application=" + self.influx_application + ","+ str(item) + "\n"
        self.__send_kpi_data(requestBody)

    def aggregated_second(self, data):
        """
        Send online data
        :param data: DataPoint
        """
        self.log.debug("Recieved data: %s", data)
        if self.send_data:
            self.kpi_buffer.append(data)

    def monitoring_data(self, data):
        if self.send_monitoring:
            self.monitoring_buffer.record_data(data)

    # Not yet implemented
    def __send_monitoring(self):
        #engine_id = self.engine.config.get('modules').get('shellexec').get('env').get('TAURUS_INDEX_ALL', '')
        #if not engine_id:
        #    engine_id = "0"
        #data = self.monitoring_buffer.get_monitoring_json(self._session)
        #self._session.send_monitoring_data(engine_id, data)
        self.log.info("Sending monitoring data...")

    NETWORK_PROBLEMS = (IOError, URLError, SSLError, ReadTimeout, TaurusNetworkError)

    def __send_kpi_data(self, data):
        """
        Sends online data to influx

        :type data: str
        """
        url = self.influx_url
        hdr = {"Content-Type": "text/plain"}
        try:
            response = requests.post(url, data, headers=hdr)
            if response.status_code > 204:
                self.log.info("Response code from Influx higher than 204. Data possibly not saved.")
                self.log.debug("Response code from Influx: %s", response)
        except (ConnectionError):
            self.log.debug("Error sending data: %s", traceback.format_exc())
            self.log.warning("Failed to send data to Influx, will retry in %s seconds", str(self.resend_timeout))
            time.sleep(self.resend_timeout)
            try:
                response = requests.post(url, data, headers=hdr)
                if response.status_code > 204:
                    self.log.info("Response code from Influx higher than 204. Data possibly not saved.")
                    self.log.debug("Response code from Influx: %s", response)
            except (ConnectionError) :
                self.log.error("Fatal error sending data. Could not connect to Influx server. Datapoints dropped.")
                self.log.debug("Fatal error sending data to Influx: %s", traceback.format_exc())

# Borrowed heavily from Blazemeter's DatapointSerializer
class InfluxDatapointSerializer(object):
    def __init__(self, owner):
        """
        :type owner: InfluxUploader
        """
        super(InfluxDatapointSerializer, self).__init__()
        self.owner = owner
        self.multi = 1000  # multiplier factor for reporting

    def get_kpi_body(self, data_buffer):
        """
        Returns a list of transaction strings to send to Influx
        """
        
        report_items = []
        if data_buffer:
            for dpoint in data_buffer: #last item is a summary. [:-1]
                time_stamp = dpoint[DataPoint.TIMESTAMP] * 1000
                for label, kpi_set in iteritems(dpoint[DataPoint.CURRENT]):
                    if label:
                        report_items += (self.__get_transaction_strings(kpi_set, str(time_stamp), label))
        
        return report_items


    def __get_transaction_strings(self, item, time_stamp, label):
        """ 
        Returns the transaction details in a string as per Influx's expected format
        Sample request body for a single transaction    
        jmeter,application=influx_testing,statut=ok,transaction=TransactionB count=1,avg=502.0,min=502.0,max=502.0,pct95.0=502.0,pct99.0=502.0,pct90.0=502.0 1544587272199000000
        """

        txn_status="ok"
        stringList=[]

        if item[KPISet.FAILURES]>0:
            txn_status="ko"

        txnString = "statut=" + txn_status + \
            ",transaction=" + label + \
            " count=" + str(item[KPISet.SAMPLE_COUNT]) + \
            ",avg=" + str(int(self.multi * item[KPISet.AVG_RESP_TIME])) + \
            ",min=" + str(int(self.multi * item[KPISet.PERCENTILES]["0.0"]) if "0.0" in item[KPISet.PERCENTILES] else "0") + \
            ",max=" + str(int(self.multi * item[KPISet.PERCENTILES]["100.0"]) if "100.0" in item[KPISet.PERCENTILES] else "0") + \
            ",pct90.0=" + str(int(item[KPISet.PERCENTILES]["90.0"]) if "90.0" in item[KPISet.PERCENTILES] else "0") + \
            ",pct95.0=" + str(int(item[KPISet.PERCENTILES]["95.0"]) if "95.0" in item[KPISet.PERCENTILES] else "0") + \
            ",pct99.0=" + str(int(item[KPISet.PERCENTILES]["99.0"]) if "99.0" in item[KPISet.PERCENTILES] else "0") + \
            ",maxAT="+ str(item[KPISet.CONCURRENCY]) + \
            ",countError=" + str(item[KPISet.FAILURES]) + \
            ",txnsum=" + str(int(self.multi * item[KPISet.AVG_RESP_TIME]) * item[KPISet.SAMPLE_COUNT]) + \
            ",txnstddev=" + str(int(self.multi * item[KPISet.STDEV_RESP_TIME])) + \
            ",ltavg=" + str(int(self.multi * item[KPISet.AVG_LATENCY])) + \
            ",bytessum=" + str(item[KPISet.BYTE_COUNT]) + \
            ",bytesavg=" + str((item[KPISet.BYTE_COUNT] / float(item[KPISet.SAMPLE_COUNT]))) + \
            " " + str(time_stamp)
        stringList.append(txnString)

        #volume details for reporting of max threads.  JMeter logs this on each transaction, however Influx expects it as a seperate row of data.
        internalString = "transaction=internal" + \
            " minAT="+ str(item[KPISet.CONCURRENCY]) + \
            ",maxAT="+ str(item[KPISet.CONCURRENCY]) + \
            ",meanAT="+ str(item[KPISet.CONCURRENCY]) + \
            ",startedT="+ str(item[KPISet.CONCURRENCY]) + \
            ",endedT=0" + \
            " " + str(time_stamp)
        stringList.append(internalString)

        #error details are reported as seperate rows in influx.
        errors = item[KPISet.ERRORS]
        errorString = ""
        for error in errors:
            errorString= "transaction=" + label + \
                ",responseMessage=" + str(error['msg']) + \
                ",responseCode=" + str(error['rc']) + \
                " count=" + str(error['cnt']) + \
                " " + str(time_stamp)
            stringList.append(errorString)
        
        return stringList