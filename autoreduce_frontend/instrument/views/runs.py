# ############################################################################### #
# Autoreduction Repository : https://github.com/ISISScientificComputing/autoreduce
#
# Copyright &copy; 2021 ISIS Rutherford Appleton Laboratory UKRI
# SPDX - License - Identifier: GPL-3.0-or-later
# ############################################################################### #

import json
import logging
import requests
from requests.exceptions import ConnectionError

from autoreduce_frontend.instrument.views.common import decode_b64, get_vars_from_run, read_variables_from_form_post_submit
# from autoreduce_db.instrument.models import InstrumentVariable
from autoreduce_db.reduction_viewer.models import Instrument, ReductionArguments, ReductionRun, Status
from autoreduce_qp.queue_processor.variable_utils import merge_arguments
from autoreduce_qp.queue_processor.reduction.service import ReductionScript
from autoreduce_qp.queue_processor.variable_utils import VariableUtils
from django.db.models.query import QuerySet
from django.shortcuts import redirect

from autoreduce_utils.settings import AUTOREDUCE_API_URL
from autoreduce_frontend.autoreduce_webapp.view_utils import (check_permissions, login_and_uows_valid, render_with)
from autoreduce_frontend.utilities import input_processing

LOGGER = logging.getLogger(__package__)


# pylint:disable=inconsistent-return-statements
@login_and_uows_valid
@check_permissions
@render_with('submit_runs.html')
def submit_runs(request, instrument=None):
    """
    Handles run submission request
    """
    LOGGER.info('Submitting runs')
    # pylint:disable=no-member
    instrument = Instrument.objects.prefetch_related('reduction_runs').get(name=instrument)
    if request.method == 'GET':
        processing_status = Status.get_processing()
        queued_status = Status.get_queued()

        # pylint:disable=no-member
        runs_for_instrument = instrument.reduction_runs.filter(batch_run=False)
        last_run = instrument.get_last_for_rerun(runs_for_instrument)

        standard_vars, advanced_vars, variable_help = get_vars_from_run(last_run)
        # pylint:disable=no-member
        context_dictionary = {
            'instrument': instrument,
            'last_instrument_run': last_run,
            'processing': runs_for_instrument.filter(status=processing_status),
            'queued': runs_for_instrument.filter(status=queued_status),
            'standard_variables': standard_vars,
            'advanced_variables': advanced_vars,
            'variable_help': variable_help
        }

        return context_dictionary


# pylint:disable=too-many-return-statements,too-many-branches,too-many-statements,too-many-locals
@login_and_uows_valid
@check_permissions
@render_with('run_confirmation.html')
def run_confirmation(request, instrument: str):
    """
    Handles request for user to confirm re-run
    """
    range_string = request.POST.get('runs')
    run_description = request.POST.get('run_description')

    # pylint:disable=no-member
    queue_count = ReductionRun.objects.filter(instrument__name=instrument, status=Status.get_queued()).count()
    context_dictionary = {
        # list stores (run_number, run_version)
        'runs': [],
        'variables': None,
        'queued': queue_count,
        'instrument_name': instrument,
        'run_description': run_description
    }

    try:
        run_numbers = input_processing.parse_user_run_numbers(range_string)
    except SyntaxError as exception:
        context_dictionary['error'] = exception.msg
        return context_dictionary

    if not run_numbers:
        context_dictionary['error'] = f"Could not correctly parse range input {range_string}"
        return context_dictionary

    # Determine user level to set a maximum limit to the number of runs that can be re-queued
    if request.user.is_superuser:
        max_runs = 500
    elif request.user.is_staff:
        max_runs = 50
    else:
        max_runs = 20

    if len(run_numbers) > max_runs:
        context_dictionary["error"] = "{0} runs were requested, but only {1} runs can be " \
                                      "queued at a time".format(len(run_numbers), max_runs)
        return context_dictionary

    related_runs: QuerySet[ReductionRun] = ReductionRun.objects.filter(
        instrument__name=instrument,
        batch_run=False,  # batch_runs are handled in BatchRunSubmit
        run_numbers__run_number__in=run_numbers)
    # Check that RB numbers are the same for the range entered
    # pylint:disable=no-member
    rb_number = related_runs.values_list('experiment__reference_number', flat=True).distinct()
    if len(rb_number) > 1:
        context_dictionary['error'] = 'Runs span multiple experiment numbers ' \
                                      '(' + ','.join(str(i) for i in rb_number) + ')' \
                                      ' please select a different range.'
        return context_dictionary

    try:
        default_variables = VariableUtils.get_default_variables(instrument)
    except (FileNotFoundError, ImportError, SyntaxError) as err:
        context_dictionary['error'] = err
        return context_dictionary

    try:
        new_script_arguments = make_reduction_arguments(request.POST.items(), default_variables)
        context_dictionary['variables'] = new_script_arguments
    except ValueError as err:
        context_dictionary['error'] = err
        return context_dictionary

    try:
        auth_token = str(request.user.auth_token)
    except AttributeError as err:
        context_dictionary['error'] = "User is not authorized to submit batch runs."
        return context_dictionary
    # run_description gets stored in run_description in the ReductionRun object
    max_run_description_length = ReductionRun._meta.get_field('run_description').max_length
    if len(run_description) > max_run_description_length:
        context_dictionary["error"] = "The description contains {0} characters, " \
                                        "a maximum of {1} are allowed".\
            format(len(run_description), max_run_description_length)
        return context_dictionary
    for run_number in run_numbers:
        matching_previous_runs = related_runs.filter(run_numbers__run_number=run_number).order_by('-run_version')
        run_suitable, reason = find_reason_to_avoid_re_run(matching_previous_runs, run_number)
        if not run_suitable:
            context_dictionary['error'] = reason
            break

        most_recent_run: ReductionRun = matching_previous_runs.first()
        # list stores (run_number, run_version)
        context_dictionary["runs"].append((run_number, most_recent_run.run_version + 1))

    try:
        response = requests.post(f"{AUTOREDUCE_API_URL}/runs/{instrument}",
                                 json={
                                     "runs": run_numbers,
                                     "reduction_arguments": new_script_arguments,
                                     "user_id": request.user.id,
                                     "description": run_description
                                 },
                                 headers={"Authorization": f"Token {auth_token}"})
        if response.status_code != 200:
            content = json.loads(response.content)
            context_dictionary['error'] = content.get("message", "Unknown error encountered")
            return context_dictionary
    except ConnectionError as err:  # pylint:disable=broad-except
        context_dictionary['error'] = "Unable to connect to the Autoreduce job submission service. If the error "\
                    "persists please let the Autoreduce team know at ISISREDUCE@stfc.ac.uk"

    except Exception as err:  # pylint:disable=broad-except
        context_dictionary['error'] = "Encountered unexpected error, "\
                f"please let the Autoreduce team know at ISISREDUCE@stfc.ac.uk: {err}"

    return context_dictionary


def find_reason_to_avoid_re_run(matching_previous_runs, run_number):
    """
    Check whether the most recent run exists
    """
    most_recent_run = matching_previous_runs.first()

    # Check old run exists - if it doesn't exist there's nothing to re-run!
    if most_recent_run is None:
        return False, f"Run number {run_number} hasn't been ran by autoreduction yet."

    # Prevent multiple queueings of the same re-run
    queued_runs = matching_previous_runs.filter(status=Status.get_queued()).first()
    if queued_runs is not None:
        return False, f"Run number {queued_runs.run_number} is already queued to run"

    return True, ""


def make_reduction_arguments(post_arguments, default_variables) -> dict:
    """
    Given new variables and the default variables create a dictionary of the new variables
    :param post_arguments: The new variables to be created
    :param default_variables: The default variables
    :return: The new variables as a dict
    :raises ValueError if any variable values exceed the allowed maximum
    """
    new_script_arguments = {"standard_vars": {}, "advanced_vars": {}}
    for key, value in post_arguments:
        if 'var-' in key:
            if 'var-advanced-' in key:
                name = key.replace('var-advanced-', '').replace('-', ' ')
                dict_key = "advanced_vars"
            elif 'var-standard-' in key:
                name = key.replace('var-standard-', '').replace('-', ' ')
                dict_key = "standard_vars"
            else:
                continue

            if name is not None:
                name = decode_b64(name)
                if name not in default_variables[dict_key]:
                    continue

                new_script_arguments[dict_key][name] = value

    if not new_script_arguments:
        raise ValueError('No variables were found to be submitted.')

    return new_script_arguments


@login_and_uows_valid
@check_permissions
@render_with('configure_new_runs.html')
def configure_new_runs(request, instrument=None, start=0, experiment_reference=0):
    """
    Handles request to view instrument variables
    """
    instrument_name = instrument

    if request.method == 'POST':
        return configure_new_runs_post(request, instrument_name)
    else:
        return configure_new_runs_get(instrument, start, experiment_reference)


def configure_new_runs_post(request, instrument_name):
    """
    Submission to modify variables. Acts on POST request.

    Depending on the parameters it either makes them for a run range (when start is given, end is optional)
    or for experiment reference (when experiment_reference is given).
    """
    start = int(request.POST.get("run_start")) if request.POST.get("run_start", None) else None
    experiment_reference = int(request.POST.get("experiment_reference_number")) if request.POST.get(
        "experiment_reference_number", None) else None

    if not start and not experiment_reference:
        return {"message": "Invalid run range or experiment reference submitted."}

    all_vars = read_variables_from_form_post_submit(request.POST)

    reduce_vars = ReductionScript(instrument_name, 'reduce_vars.py')
    reduce_vars_module = reduce_vars.load()
    try:
        args_for_range = merge_arguments(all_vars, reduce_vars_module)
    except KeyError as err:
        return {
            "message":
            f"Error encountered when processing variable with name: {err}. \
            Please check that the names of the variables in reduce_vars.py match the \
            names of the variables shown in the web app."
        }

    instrument = Instrument.objects.get(name=instrument_name)

    arguments_json = json.dumps(args_for_range, separators=(',', ':'))

    def update_or_create(instrument, arguments_json, kwargs):
        try:
            args = ReductionArguments.objects.get(instrument=instrument, **kwargs)
            args.raw = arguments_json
            args.save()
        except ReductionArguments.DoesNotExist:
            ReductionArguments.objects.create(instrument=instrument, raw=arguments_json, **kwargs)

    if start:
        update_or_create(instrument, arguments_json, {'start_run': start})
    else:
        update_or_create(instrument, arguments_json, {'experiment_reference': experiment_reference})
    return redirect('instrument:variables_summary', instrument=instrument_name)


# pylint:disable=too-many-locals
def configure_new_runs_get(instrument_name, start=0, experiment_reference=0):
    """
    GET for the configure new runs page
    """
    instrument = Instrument.objects.get(name__iexact=instrument_name)

    editing = (start > 0 or experiment_reference > 0)

    last_run = instrument.get_last_for_rerun()

    standard_vars, advanced_vars, variable_help = get_vars_from_run(last_run)
    run_start = start if start else last_run.run_number + 1

    context_dictionary = {
        'instrument': instrument,
        'last_instrument_run': last_run,
        'processing': ReductionRun.objects.filter(instrument=instrument, status=Status.get_processing()),
        'queued': ReductionRun.objects.filter(instrument=instrument, status=Status.get_queued()),
        'standard_variables': standard_vars,
        'advanced_variables': advanced_vars,
        'variable_help': variable_help,
        'run_start': run_start,
        # used to determine whether the current form is for an experiment reference
        'current_experiment_reference': experiment_reference,
        # used to create the link to an experiment reference form, using this number
        'submit_for_experiment_reference': last_run.experiment.reference_number,
        'minimum_run_start': run_start,
        'minimum_run_end': run_start + 1,
        'upcoming_run_variables': "",
        'editing': editing,
        'tracks_script': '',
    }

    return context_dictionary
