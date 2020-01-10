# ############################################################################### #
# Autoreduction Repository : https://github.com/ISISScientificComputing/autoreduce
#
# Copyright &copy; 2020 ISIS Rutherford Appleton Laboratory UKRI
# SPDX - License - Identifier: GPL-3.0-or-later
# ############################################################################### #

"""
The purpose of this script is for performing MySQL queries to monitor system state performance and health.
db_state_checks
"""

from utils.clients.database_client import DatabaseClient
from utils.clients.connection_exception import ConnectionException
import itertools
import logging


class DatabaseMonitorChecks:
    """Class to query system performance"""
    table = "reduction_viewer_reductionrun"

    # Establishing a connection with Database using Database Client
    def __init__(self):
        self.database = DatabaseClient()
        try:
            self.connection = self.database.connect()
        except ConnectionException:
            raise ConnectionException('database')

    def query_log_and_execute(self, constructed_query):
        """Logs and executes all queries ran in script"""
        logging.info('SQL QUERY: {}'.format(constructed_query))
        print(constructed_query)
        return self.connection.execute(constructed_query).fetchall()

    def instruments_list(self):
        """Retrieve current list of instruments"""
        all_instruments = "SELECT id, name "\
                          "FROM reduction_viewer_instrument"
        return self.query_log_and_execute(all_instruments)

    def missing_rb_report(self, instrument, start_date, end_date):
        """Retrieves run_number column and return missing sequential values """
        missing_rb_calc_vars = {}

        missing_rb_query = "SELECT run_number "\
                           "FROM {} " \
                           "WHERE instrument_id = {} " \
                           "AND created " \
                           "BETWEEN '{}' " \
                           "AND '{}'".format(DatabaseMonitorChecks.table,
                                                              instrument,
                                                              start_date,
                                                              end_date)
        missing_rb_calc_vars['run_numbers'] = self.query_log_and_execute(missing_rb_query)

        # Converts list of run number sets containing longs into list of integers [(123L), (456L)] -> [123, 456]
        return [int(elem) for elem in list(itertools.chain.from_iterable(missing_rb_calc_vars['run_numbers']))]

    @staticmethod
    def query_sub_segment_replace(query_arguments, start_date, end_date):
        """Select last query argument based on argument input - sub_segment selection"""
        interval_range = "INTERVAL {} {}".format(query_arguments['interval'], query_arguments['time_scale'])
        date_range = "BETWEEN '{}' AND '{}'".format(start_date, end_date)
        if not query_arguments['start_date']:
            query_sub_segment = ">= DATE_SUB('{}', {})".format(end_date, interval_range)
        else:
            # When both start and end date inputs are populated, query between those dates.
            query_sub_segment = date_range
        return query_sub_segment

    def query_segment_replace(self, query_arguments, start_date, end_date):
        """Handles the interchangeable segment of query to return either intervals of time or period between two
        user specified dates and whether or not to include a filter by retry run or not."""

        # If end date is None, query only for rows created on current date
        returned_args = []
        current_date = 'CURDATE()'

        if query_arguments['start_date'] == query_arguments['end_date']:
            query_segment = "= {}".format(current_date)
            query_arguments['start_date'] = ''
            returned_args.append(query_segment)

        else:
            # Determining which sub query segment to place in query.
            query_segment = self.query_sub_segment_replace(query_arguments, start_date, end_date)
            returned_args.append(query_segment)

        if not query_arguments['instrument_id']:
            # Removing relevant instrument_id query argument segments when not specified as method arg
            instrument_id_arg = ""
            query_arguments['instrument_id'] = ''
            returned_args.append(instrument_id_arg)

        else:
            # Applying instrument_id query argument segments when instrument_id argument populated as method arg
            instrument_id_arg = ", instrument_id"
            query_arguments['instrument_id'] = ', {}'.format(query_arguments['instrument_id'])
            returned_args.append(instrument_id_arg)

        return [returned_args]

    def get_data_by_status_over_time(self, selection='run_number', status_id=4, retry_run='',
                                     anomic_aphasia='finished', end_date='CURDATE()', interval=1, time_scale='DAY',
                                     start_date=None, instrument_id=None):
        """
        Default Variables
        :param selection : * Which column you would like to select or all columns by default
        :param status_id : 1 Interchangeable id to look at different run status's
        :param retry_run : Whether or not a user is looking for runs that have been retried
        :param instrument_id : the instrument id of the instrument to be queried.
        :param anomic_aphasia : "finished" DateTime column in database (created, last_updated, started, finished)
        :param end_date : Most recent date you wish to query up too. By default this is the current date.
        :param interval : 1 Interval for time_scale
        :param time_scale : "DAY" Expected inputs include DAY, Month or YEAR
        :param start_date : The furthest date from today you wish to query from e.g the start of start of cycle.
        """

        def _query_out(instrument_id_arg, query_type_segment):
            """Executes and returns built query as list"""
            query_argument = "SELECT {} " \
                 "FROM {} " \
                 "WHERE (status_id {}) = ({} {}) {} " \
                 "AND {} {}".format(arguments['selection'],
                                    DatabaseMonitorChecks.table,
                                    instrument_id_arg,
                                    arguments['status_id'],
                                    arguments['instrument_id'],
                                    arguments['retry_run'],
                                    arguments['anomic_aphasia'],
                                    query_type_segment)

            return [list(elem) for elem in self.query_log_and_execute(query_argument)]

        arguments = locals()  # Retrieving user specified variables
        # Determining query segment to use
        interchangeable_query_args = self.query_segment_replace(arguments, start_date=start_date, end_date=end_date)
        return _query_out(interchangeable_query_args[0][1], interchangeable_query_args[0][0])

# Hard coded queries for manual testing only and to be removed before full integration

# print(DatabaseMonitorChecks().get_data_by_status_over_time(instrument_id=6, end_date='CURDATE()', start_date='CURDATE()'))
# print(DatabaseMonitorChecks().get_data_by_status_over_time(instrument_id=6, end_date='2019-12-13', start_date='2019-12-12'))
# print(DatabaseMonitorChecks().get_data_by_status_over_time(selection='COUNT(id)', instrument_id=8))
# print(DatabaseMonitorChecks().instruments_list())
# DatabaseMonitorChecks().missing_rb_report(7, start_date='2019:11:12', end_date='2019:12:13')

# print(DatabaseMonitorChecks().get_data_by_status_over_time(selection="id, "
#                                                                      "run_number, "
#                                                                      "DATE_FORMAT(started, '%H:%i:%s') TIMEONLY,"
#                                                                      "DATE_FORMAT(finished, '%H:%i:%s') TIMEONLY",
#                                                            instrument_id=7,
#                                                            anomic_aphasia='created',
#                                                            end_date='2019-12-13',
#                                                            start_date='2019-12-12'))
