import io
import os
import asyncio
import datetime  # noqa: 251
from typing import Any, List
from unittest.mock import patch
import pytest
import requests_mock

import dlt

from dlt.common import json
from dlt.common.configuration.specs import CredentialsConfiguration
from dlt.common.configuration.specs.config_providers_context import ConfigProvidersContext
from dlt.common.pipeline import ExtractInfo
from dlt.common.schema import Schema
from dlt.common.runtime.telemetry import stop_telemetry
from dlt.common.typing import DictStrAny, StrStr, TSecretValue

from dlt.pipeline.exceptions import PipelineStepFailed
from dlt.pipeline.pipeline import Pipeline
from dlt.pipeline.trace import PipelineTrace, SerializableResolvedValueTrace, describe_extract_data, load_trace
from dlt.pipeline.track import slack_notify_load_success
from dlt.extract.source import DltResource, DltSource
from dlt.extract.pipe import Pipe

from tests.utils import start_test_telemetry
from tests.common.configuration.utils import toml_providers, environment

def test_create_trace(toml_providers: ConfigProvidersContext) -> None:

    @dlt.source
    def inject_tomls(api_type = dlt.config.value, credentials: CredentialsConfiguration = dlt.secrets.value, secret_value: TSecretValue = "123"):

        @dlt.resource
        def data():
            yield [1, 2, 3]

        return data()

    p = dlt.pipeline(destination="dummy")

    # read from secrets and configs directly
    databricks_creds = "databricks+connector://token:<databricks_token>@<databricks_host>:443/<database_or_schema_name>?conn_timeout=15&search_path=a,b,c"
    s = dlt.secrets["databricks.credentials"]
    assert s == databricks_creds

    extract_info = p.extract(inject_tomls())
    trace = p.last_trace
    assert trace is not None
    assert p._trace is None
    assert len(trace.steps) == 1
    step = trace.steps[0]
    assert step.step == "extract"
    assert isinstance(step.started_at, datetime.datetime)
    assert isinstance(step.finished_at, datetime.datetime)
    assert isinstance(step.step_info, ExtractInfo)
    assert step.step_info.extract_data_info == [{"name": "inject_tomls", "data_type": "source"}]
    # check config trace
    resolved = _find_resolved_value(trace.resolved_config_values, "api_type", [])
    assert resolved.config_type_name == "TestCreateTraceInjectTomlsConfiguration"
    assert resolved.value == "REST"
    assert resolved.is_secret_hint is False
    assert resolved.default_value is None
    assert resolved.provider_name == "config.toml"
    # dictionaries are not returned anymore
    resolved = _find_resolved_value(trace.resolved_config_values, "credentials", [])
    assert resolved is None or isinstance(resolved.value, str)
    resolved = _find_resolved_value(trace.resolved_config_values, "secret_value", [])
    assert resolved.is_secret_hint is True
    assert resolved.value == "2137"
    assert resolved.default_value == "123"
    resolved = _find_resolved_value(trace.resolved_config_values, "credentials", ["databricks"])
    assert resolved.is_secret_hint is True
    assert resolved.value == databricks_creds
    assert_trace_printable(trace)

    # extract with exception
    @dlt.source
    def async_exception(max_range=1):

        async def get_val(v):
            await asyncio.sleep(0.1)
            if v % 3 == 0:
                raise ValueError(v)
            return v

        @dlt.resource
        def data():
            yield from [get_val(v) for v in range(1,max_range)]

        return data()

    with pytest.raises(PipelineStepFailed):
        p.extract(async_exception())

    trace = p.last_trace
    assert p._trace is None
    assert len(trace.steps) == 2
    step = trace.steps[1]
    assert step.step == "extract"
    assert isinstance(step.step_exception, str)
    assert isinstance(step.step_info, ExtractInfo)
    assert step.step_info.extract_data_info == [{"name": "async_exception", "data_type": "source"}]
    assert_trace_printable(trace)

    # normalize
    norm_info = p.normalize()
    trace = p.last_trace
    assert p._trace is None
    assert len(trace.steps) == 3
    step = trace.steps[2]
    assert step.step == "normalize"
    assert step.step_info is norm_info
    assert_trace_printable(trace)

    # load
    os.environ["COMPLETED_PROB"] = "1.0"  # make it complete immediately
    load_info = p.load()
    trace = p.last_trace
    assert p._trace is None
    assert len(trace.steps) == 4
    step = trace.steps[3]
    assert step.step == "load"
    assert step.step_info is load_info
    resolved = _find_resolved_value(trace.resolved_config_values, "completed_prob", [])
    assert resolved.is_secret_hint is False
    assert resolved.value == "1.0"
    assert resolved.config_type_name == "DummyClientConfiguration"
    assert_trace_printable(trace)

    # run resets the trace
    load_info = inject_tomls().run()
    trace = p.last_trace
    assert p._trace is None
    assert len(trace.steps) == 4  # extract, normalize, load, run
    step = trace.steps[-1]  # the last one should be run
    assert step.step == "run"
    assert step.step_info is load_info
    assert trace.steps[0].step_info is not extract_info

    step = trace.steps[-2]  # the previous one should be load
    assert step.step == "load"
    assert step.step_info is load_info  # same load info
    assert trace.steps[0].step_info is not extract_info
    assert_trace_printable(trace)


def test_save_load_trace() -> None:
    os.environ["COMPLETED_PROB"] = "1.0"
    info = dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
    pipeline = dlt.pipeline()
    # will get trace from working dir
    trace = pipeline.last_trace
    assert trace is not None
    assert pipeline._trace is None
    assert len(trace.steps) == 4 == len(info.pipeline.last_trace.steps)
    step = trace.steps[-2]  # the previoius to last one should be load
    assert step.step == "load"
    resolved = _find_resolved_value(trace.resolved_config_values, "completed_prob", [])
    assert resolved.is_secret_hint is False
    assert resolved.value == "1.0"
    assert resolved.config_type_name == "DummyClientConfiguration"
    assert_trace_printable(trace)

    # exception also saves trace
    @dlt.resource
    def data():
        raise NotImplementedError()
        yield

    with pytest.raises(PipelineStepFailed) as py_ex:
        dlt.run(data(), destination="dummy")
    # there's the same pipeline in exception as in previous run
    assert py_ex.value.pipeline is info.pipeline
    trace = load_trace(py_ex.value.pipeline.working_dir)
    assert trace is not None
    assert pipeline._trace is None
    assert len(trace.steps) == 2  # extract with exception, also has run with exception
    step = trace.steps[-2]
    assert step.step == "extract"
    assert step.step_exception is not None
    run_step = trace.steps[-1]
    assert run_step.step == "run"
    assert run_step.step_exception is not None
    assert step.step_exception == run_step.step_exception
    assert_trace_printable(trace)


def test_disable_trace(environment: StrStr) -> None:
    environment["ENABLE_RUNTIME_TRACE"] = "false"
    environment["COMPLETED_PROB"] = "1.0"
    dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
    assert dlt.pipeline().last_trace is None


def test_trace_on_restore_state(environment: StrStr) -> None:
    environment["COMPLETED_PROB"] = "1.0"

    def _sync_destination_patch(self: Pipeline, destination: str = None, dataset_name: str = None):
        # just wipe the pipeline simulating deleted dataset
        self._wipe_working_folder()
        self._configure(self._schema_storage_config.export_schema_path, self._schema_storage_config.import_schema_path, False)

    with patch.object(Pipeline, 'sync_destination', _sync_destination_patch):
        dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
        assert len(dlt.pipeline().last_trace.steps) == 4


def test_load_none_trace() -> None:
    p = dlt.pipeline()
    assert load_trace(p.working_dir) is None


def test_trace_telemetry() -> None:
    with patch("dlt.common.runtime.sentry.before_send", _mock_sentry_before_send), patch("dlt.common.runtime.segment.before_send", _mock_segment_before_send):
        start_test_telemetry()

        SEGMENT_SENT_ITEMS.clear()
        SENTRY_SENT_ITEMS.clear()
        # default dummy fails all files
        dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
        # we should have 4 segment items
        assert len(SEGMENT_SENT_ITEMS) == 4
        expected_steps = ["extract", "normalize", "load", "run"]
        for event, step in zip(SEGMENT_SENT_ITEMS, expected_steps):
            assert event["event"] == f"pipeline_{step}"
            assert event["properties"]["success"] is True
            assert event["properties"]["destination_name"] == "dummy"
            assert isinstance(event["properties"]["elapsed"], float)
            assert isinstance(event["properties"]["transaction_id"], str)
            # check extract info
            if step == "extract":
                assert event["properties"]["extract_data"] == [{"name": "", "data_type": "int"}]
            if step == "load":
                # dummy has empty fingerprint
                assert event["properties"]["destination_fingerprint"] == ""
        # we have two failed files (state and data) that should be logged by sentry
        assert len(SENTRY_SENT_ITEMS) == 2

        # trace with exception
        @dlt.resource
        def data():
            raise NotImplementedError()
            yield

        SEGMENT_SENT_ITEMS.clear()
        SENTRY_SENT_ITEMS.clear()
        with pytest.raises(PipelineStepFailed):
            dlt.pipeline().run(data, destination="dummy")
        assert len(SEGMENT_SENT_ITEMS) == 2
        event = SEGMENT_SENT_ITEMS[0]
        assert event["event"] == "pipeline_extract"
        assert event["properties"]["success"] is False
        assert event["properties"]["destination_name"] == "dummy"
        assert isinstance(event["properties"]["elapsed"], float)
        # check extract info
        if step == "extract":
            assert event["properties"]["extract_data"] == [{"name": "data", "data_type": "resource"}]
        # we didn't log any errors
        assert len(SENTRY_SENT_ITEMS) == 0


def test_extract_data_describe() -> None:
    schema = Schema("test")
    assert describe_extract_data(DltSource("sss_extract", "sect", schema)) == [{"name": "sss_extract", "data_type": "source"}]
    assert describe_extract_data(DltResource(Pipe("rrr_extract"), None, False)) == [{"name": "rrr_extract", "data_type": "resource"}]
    assert describe_extract_data([DltSource("sss_extract", "sect", schema)]) == [{"name": "sss_extract", "data_type": "source"}]
    assert describe_extract_data([DltResource(Pipe("rrr_extract"), None, False)]) == [{"name": "rrr_extract", "data_type": "resource"}]
    assert describe_extract_data(
        [DltResource(Pipe("rrr_extract"), None, False), DltSource("sss_extract", "sect", schema)]
        ) == [
            {"name": "rrr_extract", "data_type": "resource"}, {"name": "sss_extract", "data_type": "source"}
        ]
    assert describe_extract_data([{"a": "b"}]) == [{"name": "", "data_type": "dict"}]
    from pandas import DataFrame
    # we assume that List content has same type
    assert describe_extract_data([DataFrame(), {"a": "b"}]) == [{"name": "", "data_type": "DataFrame"}]
    # first unnamed element in the list breaks checking info
    assert describe_extract_data(
        [DltResource(Pipe("rrr_extract"), None, False), DataFrame(), DltSource("sss_extract", "sect", schema)]
        ) == [
            {"name": "rrr_extract", "data_type": "resource"}, {"name": "", "data_type": "DataFrame"}
        ]


def test_slack_hook(environment: StrStr) -> None:
    stop_telemetry()
    hook_url = "https://hooks.slack.com/services/T04DHMAF13Q/B04E7B1MQ1H/TDHEI123WUEE"
    environment["COMPLETED_PROB"] = "1.0"
    environment["GITHUB_USER"] = "rudolfix"
    environment["RUNTIME__DLTHUB_TELEMETRY"] = "False"
    environment["RUNTIME__SLACK_INCOMING_HOOK"] = hook_url
    with requests_mock.mock() as m:
        m.post(hook_url, json={})
        load_info = dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
        assert slack_notify_load_success(load_info.pipeline.runtime_config.slack_incoming_hook, load_info, load_info.pipeline.last_trace) == 200
    assert m.called
    message = m.last_request.json()
    assert "rudolfix" in message["text"]
    assert "dummy" in message["text"]


def test_broken_slack_hook(environment: StrStr) -> None:
    environment["COMPLETED_PROB"] = "1.0"
    environment["RUNTIME__SLACK_INCOMING_HOOK"] = "http://localhost:22"
    load_info = dlt.pipeline().run([1,2,3], table_name="data", destination="dummy")
    # connection error
    assert slack_notify_load_success(load_info.pipeline.runtime_config.slack_incoming_hook, load_info, load_info.pipeline.last_trace) == -1
    # pipeline = dlt.pipeline()
    # assert pipeline.last_trace is not None
    # assert pipeline._trace is None
    # trace = load_trace(info.pipeline.working_dir)
    # assert len(trace.steps) == 4
    # run_step = trace.steps[-1]
    # assert run_step.step == "run"
    # assert run_step.step_exception is None


def _find_resolved_value(resolved: List[SerializableResolvedValueTrace], key: str, sections: List[str]) -> SerializableResolvedValueTrace:
    return next((v for v in resolved if v.key == key and v.sections == sections), None)


SEGMENT_SENT_ITEMS = []
def _mock_segment_before_send(event: DictStrAny) -> DictStrAny:
    SEGMENT_SENT_ITEMS.append(event)
    return event


SENTRY_SENT_ITEMS = []
def _mock_sentry_before_send(event: DictStrAny, _unused_hint: Any = None) -> DictStrAny:
    SENTRY_SENT_ITEMS.append(event)
    return event

def assert_trace_printable(trace: PipelineTrace) -> None:
    str(trace)
    trace.asstr(0)
    trace.asstr(1)
    json.dumps(trace)
    with io.BytesIO() as b:
        json.typed_dump(trace, b)
        b.getvalue()
