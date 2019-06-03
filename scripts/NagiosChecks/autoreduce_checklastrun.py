# ############################################################################### #
# Autoreduction Repository : https://github.com/ISISScientificComputing/autoreduce
#
# Copyright &copy; 2019 ISIS Rutherford Appleton Laboratory UKRI
# SPDX - License - Identifier: GPL-3.0-or-later
# ############################################################################### #
#! /usr/bin/env python
"""
Check that the last run is correct
"""
from __future__ import print_function
import sys
from os import path

import MySQLdb
import MySQLdb.cursors

from scripts.NagiosChecks.autoreduce_settings import MYSQL, ISIS_MOUNT


# pylint: disable=invalid-name
def checkLastRun():
    """
    Compares the last run in the database to the lastrun.txt file
    :return: 0 - Success
             2 - Failure
    """
    message = ""
    db = MySQLdb.connect(host=MYSQL['host'], port=3306,
                         user=MYSQL['username'], passwd=MYSQL['password'],
                         db=MYSQL['db'], cursorclass=MySQLdb.cursors.DictCursor)
    cursor = db.cursor()

    # Get Instruments
    instruments = []
    cursor.execute("SELECT id," +
                   "name FROM reduction_viewer_instrument WHERE is_active = 1 " +
                   "AND is_paused = 0")
    for i in cursor.fetchall():
        instruments.append(i)

    # Get last reduced datafile run number
    for inst in instruments:
        cursor.execute("SELECT MAX(run_number)" +
                       "FROM reduction_viewer_reductionrun WHERE instrument_id = "
                       + str(inst['id']))
        last_reduction_run = cursor.fetchone()['MAX(run_number)']
        last_run_file = open(path.join(ISIS_MOUNT, "NDX" + inst['name'],
                                       "Instrument", "logs", "lastrun.txt"),
                             "r")
        last_run = int(last_run_file.readline().split(' ')[1])

        # Check range because it may be a couple out.
        if last_reduction_run not in range(last_run-2, last_run+2):
            message += inst['name'] + " - last_run.txt = " + str(last_run) + \
                       " reduction run = " + str(last_reduction_run) + ". "

    db.close()
    if message:
        print(message)
        return 2
    return 0

# pylint: disable=using-constant-test
if "__name__":
    sys.exit(checkLastRun())
