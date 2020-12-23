# ############################################################################
# Autoreduction Repository :
# https://github.com/ISISScientificComputing/autoreduce
#
# Copyright &copy; 2020 ISIS Rutherford Appleton Laboratory UKRI
# SPDX - License - Identifier: GPL-3.0-or-later
# ############################################################################
"""
This modules handles an incoming message from the queue processor
and takes appropriate action(s) based on the message contents.

For example, this may include shuffling the message to another queue,
update relevant DB fields or logging out the status.
"""
import datetime
import logging
from django.db import transaction, IntegrityError

import model.database.records as db_records
from model.database import access as db_access
from model.message.message import Message
from model.message.validation.validators import validate_rb_number
from queue_processors.queue_processor._utils_classes import _UtilsClasses
from queue_processors.queue_processor.queueproc_utils.script_utils import get_current_script_text
from queue_processors.queue_processor.handling_exceptions import InvalidStateException
from utils.settings import ACTIVEMQ_SETTINGS
from .reduction_runner.reduction_process_manager import ReductionProcessManager


class HandleMessage:
    """
    Handles messages from the queue client and forwards through various
    stages depending on the message contents.
    """

    # We cannot type hint queue listener without introducing a circular dep.
    def __init__(self, queue_listener):
        self._client = queue_listener
        self._utils = _UtilsClasses()

        self._logger = logging.getLogger("handle_queue_message")

        self._cached_db = None
        self._cached_data_model = None

    @property
    def _database(self):
        """
        Gets a handle to the database, starting it if required
        """
        if not self._cached_db:
            self._cached_db = db_access.start_database()
        return self._cached_db

    @property
    def _data_model(self):
        """
        Gets a handle to the data model from the database
        """
        if not self._cached_data_model:
            self._cached_data_model = self._database.data_model
        return self._cached_data_model

    def data_ready(self, message: Message):
        """
        Called when destination queue was data_ready.
        Updates the reduction run in the database.

        When we DO NO PROCESSING:
        - If rb number isn't an integer, or isn't a 7 digit integer
        - If instrument is paused
        """
        self._logger.info("Data ready for processing run %s on %s", message.run_number, message.instrument)
        if not validate_rb_number(message.rb_number):
            # rb_number is invalid so send message to skip queue and early return
            message.message = f"Found non-integer RB number: {message.rb_number}"
            self._logger.warning("%s. Skipping %s%s.", message.message, message.instrument, message.run_number)
            message.rb_number = 0

        run_no = str(message.run_number)
        instrument = self._get_and_activate_db_inst(message.instrument)

        status = self._utils.status.get_skipped() if instrument.is_paused \
            else self._utils.status.get_queued()

        # This must be done before looking up the run version to make sure
        # the record exists
        experiment = db_access.get_experiment(message.rb_number, create=True)
        run_version = db_access.find_highest_run_version(run_number=run_no, experiment=experiment)
        message.run_version = run_version

        # Get the script text for the current instrument. If the script text
        # is null then send to
        # error queue
        script_text = get_current_script_text(instrument.name)[0]
        if script_text is None:
            self.reduction_error(message)
            raise InvalidStateException("Script text for current instrument is null")

        # Make the new reduction run with the information collected so far
        # and add it into the database
        reduction_run = db_records.create_reduction_run_record(experiment=experiment,
                                                               instrument=instrument,
                                                               message=message,
                                                               run_version=run_version,
                                                               script_text=script_text,
                                                               status=status)
        self.safe_save(reduction_run)

        # Create a new data location entry which has a foreign key linking it to the current
        # reduction run. The file path itself will point to a datafile
        # (e.g. "\isis\inst$\NDXWISH\Instrument\data\cycle_17_1\WISH00038774 .nxs")
        data_location = self._data_model.DataLocation(file_path=message.data, reduction_run_id=reduction_run.id)
        self.safe_save(data_location)

        # Create all of the variables for the run that are described in it's reduce_vars.py
        self._logger.info('Creating variables for run')
        try:
            variables = self._utils.instrument_variable.create_variables_for_run(reduction_run)
            if not variables:
                self._logger.warning("No instrument variables found on %s for run %s", instrument.name,
                                     message.run_number)
        except IntegrityError as err:
            # couldn't save the state in the database properly
            self._logger.error("Encountered error in transaction to save RunVariables, error: %s", str(err))
            raise

        self._logger.info('Getting script and arguments')
        reduction_script, arguments = self._utils.reduction_run.get_script_and_arguments(reduction_run)
        message.reduction_script = reduction_script
        message.reduction_arguments = arguments

        return self.send_message_onwards(reduction_run, message, instrument)

    def safe_save(self, obj):
        try:
            with transaction.atomic():
                return obj.save()
        except IntegrityError as err:
            # couldn't save the state in the database
            self._logger.error("Encountered error in transaction, error: %s", str(err))
            raise

    def send_message_onwards(self, reduction_run, message, instrument):
        try:
            message.validate("/queue/DataReady")
        except RuntimeError as validation_err:
            self._logger.error("Validation error from handler: %s", str(validation_err))
            self.reduction_skipped(reduction_run, message)
            return

        if instrument.is_paused:
            self._logger.info("Run %s has been skipped because the instrument %s is paused", message.run_number,
                              instrument.name)
            self.reduction_skipped(reduction_run, message)
        else:
            # success branch
            self._logger.info("Run %s ready for reduction", message.run_number)
            self.do_reduction(reduction_run, message)

    def do_reduction(self, reduction_run, message):
        pr = ReductionProcessManager(message)
        self.reduction_started(reduction_run, message)
        process_finished, message, err = pr.run()
        if process_finished:
            if message.message is not None:
                self.reduction_error(reduction_run, message)
            else:
                self.reduction_complete(reduction_run, message)
        else:
            # subprocess exited with 1 - any unexpected errors encountered will be caught here
            # Note that this doesn't handle any errors inside the reduce.py itself, the error
            # needs to have happened in the setup code before or after the reduce.py execution
            self._logger.error("Encountered an error while trying to start the reduction process %s", err)

    def _get_and_activate_db_inst(self, instrument_name):
        """
        Gets the DB instrument record from the database, if one is not
        found it instead creates and saves the record to the DB, then
        returns it.
        """
        # Check if the instrument is active or not in the MySQL database
        instrument = db_access.get_instrument(str(instrument_name), create=True)
        # Activate the instrument if it is currently set to inactive
        if not instrument.is_active:
            self._logger.info("Activating %s", instrument_name)
            instrument.is_active = 1
            instrument.save()
        return instrument

    # note: Why does this take arguments and not just take from the message attribs
    def _construct_and_send_skipped(self, rb_number, reason, message: Message):
        """
        Construct a message and send to the skipped reduction queue
        :param rb_number: The RB Number associated with the reduction job
        :param reason: The error that caused the run to be skipped
        """
        self._logger.warning("Skipping non-integer RB number: %s", rb_number)
        msg = 'Reduction Skipped: {}. Assuming run number to be ' \
              'a calibration run.'.format(reason)
        message.message = msg
        skipped_queue = ACTIVEMQ_SETTINGS.reduction_skipped
        self._client.send_message(skipped_queue, message)

    def reduction_started(self, reduction_run, message: Message):
        """
        Called when destination queue was reduction_started.
        Updates the run as started in the database.
        """
        self._logger.info("Run %s has started reduction", message.run_number)

        if reduction_run.status.value not in ['e', 'q']:  # verbose values = ["Error", "Queued"]
            raise InvalidStateException("An invalid attempt to re-start a reduction run was captured."
                                        f" Experiment: {message.rb_number},"
                                        f" Run Number: {message.run_number},"
                                        f" Run Version {message.run_version}")

        reduction_run.status = self._utils.status.get_processing()
        reduction_run.started = datetime.datetime.utcnow()
        self.safe_save(reduction_run)

    def reduction_complete(self, reduction_run, message: Message):
        """
        Called when the destination queue was reduction_complete
        Updates the run as complete in the database.
        """
        self._logger.info("Run %s has completed reduction", message.run_number)

        if not reduction_run.status.value == 'p':  # verbose value = "Processing"
            raise InvalidStateException("An invalid attempt to complete a reduction run that wasn't"
                                        " processing has been captured. "
                                        f" Experiment: {message.rb_number},"
                                        f" Run Number: {message.run_number},"
                                        f" Run Version {message.run_version}")

        self._common_reduction_run_update(reduction_run, self._utils.status.get_completed(), message)

        if message.reduction_data is not None:
            for location in message.reduction_data:
                model = db_access.start_database().data_model
                reduction_location = model \
                    .ReductionLocation(file_path=location,
                                       reduction_run=reduction_run)
                self.safe_save(reduction_location)
        self.safe_save(reduction_run)

    def reduction_skipped(self, reduction_run, message: Message):
        """
        Called when the destination was reduction skipped
        Updates the run to Skipped status in database
        Will NOT attempt re-run
        """
        if message.message is not None:
            self._logger.info("Run %s has been skipped - %s", message.run_number, message.message)
        else:
            self._logger.info("Run %s has been skipped - No error message was found", message.run_number)

        self._common_reduction_run_update(reduction_run, self._utils.status.get_skipped(), message)
        self.safe_save(reduction_run)

    def reduction_error(self, reduction_run, message: Message):
        """
        Called when the destination was reduction_error.
        Updates the run as complete in the database.
        """
        if message.message:
            self._logger.info("Run %s has encountered an error - %s", message.run_number, message.message)
        else:
            self._logger.info("Run %s has encountered an error - No error message was found", message.run_number)

        self._common_reduction_run_update(reduction_run, self._utils.status.get_error(), message)
        self.safe_save(reduction_run)

    @staticmethod
    def _common_reduction_run_update(reduction_run, status, message):
        reduction_run.status = status
        reduction_run.finished = datetime.datetime.utcnow()
        reduction_run.message = message.message
        reduction_run.reduction_log = message.reduction_log
        reduction_run.admin_log = message.admin_log
