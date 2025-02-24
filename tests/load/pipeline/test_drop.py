from typing import Any, Iterator, Dict, Any, List
from unittest import mock
from itertools import chain

import pytest

import dlt
from dlt.extract.source import DltResource
from dlt.common.utils import uniq_id
from dlt.pipeline import helpers, state_sync, Pipeline
from dlt.load import Load
from dlt.pipeline.exceptions import PipelineStepFailed
from dlt.destinations.job_client_impl import SqlJobClientBase

from tests.utils import ALL_DESTINATIONS


def _attach(pipeline: Pipeline) -> Pipeline:
    return dlt.attach(pipeline.pipeline_name, pipeline.pipelines_dir)


@dlt.source(section='droppable', name='droppable')
def droppable_source() -> List[DltResource]:
    @dlt.resource
    def droppable_a(a: dlt.sources.incremental[int]=dlt.sources.incremental('a', 0)) -> Iterator[Dict[str, Any]]:
        yield dict(a=1, b=2, c=3)
        yield dict(a=4, b=23, c=24)


    @dlt.resource
    def droppable_b(asd: dlt.sources.incremental[int]=dlt.sources.incremental('asd', 0)) -> Iterator[Dict[str, Any]]:
        # Child table
        yield dict(asd=2323, qe=555, items=[dict(m=1, n=2), dict(m=3, n=4)])


    @dlt.resource
    def droppable_c(qe: dlt.sources.incremental[int] = dlt.sources.incremental('qe')) -> Iterator[Dict[str, Any]]:
        # Grandchild table
        yield dict(asdasd=2424, qe=111, items=[
            dict(k=2, r=2, labels=[dict(name='abc'), dict(name='www')])
        ])

    @dlt.resource
    def droppable_d(o: dlt.sources.incremental[int] = dlt.sources.incremental('o')) -> Iterator[List[Dict[str, Any]]]:
        dlt.state()['data_from_d'] = {'foo1': {'bar': 1}, 'foo2': {'bar': 2}}
        yield [dict(o=55), dict(o=22)]

    return [droppable_a(), droppable_b(), droppable_c(), droppable_d()]


RESOURCE_TABLES = dict(
    droppable_a=['droppable_a'],
    droppable_b=['droppable_b', 'droppable_b__items'],
    droppable_c=['droppable_c', 'droppable_c__items', 'droppable_c__items__labels'],
    droppable_d=['droppable_d']
)


def assert_dropped_resources(pipeline: Pipeline, resources: List[str]) -> None:
    assert_dropped_resource_tables(pipeline, resources)
    assert_dropped_resource_states(pipeline, resources)

def assert_dropped_resource_tables(pipeline: Pipeline, resources: List[str]) -> None:
    # Verify only requested resource tables are removed from pipeline schema
    all_tables = set(chain.from_iterable(RESOURCE_TABLES.values()))
    dropped_tables = set(chain.from_iterable(RESOURCE_TABLES[r] for r in resources))
    expected_tables = all_tables - dropped_tables
    result_tables = set(t['name'] for t in pipeline.default_schema.data_tables())
    assert result_tables == expected_tables

    # Verify requested tables are dropped from destination
    client: SqlJobClientBase
    with pipeline._get_destination_client(pipeline.default_schema) as client:  # type: ignore[assignment]
        # Check all tables supposed to be dropped are not in dataset
        for table in dropped_tables:
            exists, _ = client.get_storage_table(table)
            assert not exists
        # Check tables not from dropped resources still exist
        for table in expected_tables:
            exists, _ = client.get_storage_table(table)
            assert exists


def assert_dropped_resource_states(pipeline: Pipeline, resources: List[str]) -> None:
    # Verify only requested resource keys are removed from state
    all_resources = set(RESOURCE_TABLES.keys())
    expected_keys = all_resources - set(resources)
    sources_state = pipeline.state['sources']  # type: ignore[typeddict-item]
    result_keys = set(sources_state['droppable']['resources'].keys())
    assert result_keys == expected_keys


def assert_destination_state_loaded(pipeline: Pipeline) -> None:
    """Verify stored destination state matches the local pipeline state"""
    with pipeline.sql_client() as sql_client:
        destination_state = state_sync.load_state_from_destination(pipeline.pipeline_name, sql_client)
    pipeline_state = dict(pipeline.state)
    del pipeline_state['_local']
    assert pipeline_state == destination_state


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_drop_command_resources_and_state(destination_name: str) -> None:
    """Test the drop command with resource and state path options and
    verify correct data is deleted from destination and locally"""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)
    helpers.drop(attached, resources=['droppable_c', 'droppable_d'], state_paths='data_from_d.*.bar')

    attached = _attach(pipeline)

    assert_dropped_resources(attached, ['droppable_c', 'droppable_d'])

    # Verify extra json paths are removed from state
    sources_state = pipeline.state['sources']  # type: ignore[typeddict-item]
    assert sources_state['droppable']['data_from_d'] == {'foo1': {}, 'foo2': {}}

    assert_destination_state_loaded(pipeline)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_drop_destination_tables_fails(destination_name: str) -> None:
    """Fail on drop tables. Command runs again."""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)

    with mock.patch.object(helpers.DropCommand, '_drop_destination_tables', side_effect=RuntimeError("Something went wrong")):
        with pytest.raises(RuntimeError):
            helpers.drop(attached, resources=('droppable_a', 'droppable_b'))

    attached = _attach(pipeline)
    helpers.drop(attached, resources=('droppable_a', 'droppable_b'))

    assert_dropped_resources(attached, ['droppable_a', 'droppable_b'])
    assert_destination_state_loaded(attached)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_fail_after_drop_tables(destination_name: str) -> None:
    """Fail directly after drop tables. Command runs again ignoring destination tables missing."""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)

    with mock.patch.object(helpers.DropCommand, '_drop_state_keys', side_effect=RuntimeError("Something went wrong")):
        with pytest.raises(RuntimeError):
            helpers.drop(attached, resources=('droppable_a', 'droppable_b'))

    attached = _attach(pipeline)
    helpers.drop(attached, resources=('droppable_a', 'droppable_b'))

    assert_dropped_resources(attached, ['droppable_a', 'droppable_b'])
    assert_destination_state_loaded(attached)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_load_step_fails(destination_name: str) -> None:
    """Test idempotency. pipeline.load() fails. Command can be run again successfully"""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)

    with mock.patch.object(Load, 'run', side_effect=RuntimeError("Something went wrong")):
        with pytest.raises(PipelineStepFailed) as e:
            helpers.drop(attached, resources=('droppable_a', 'droppable_b'))
        assert isinstance(e.value.exception, RuntimeError)

    attached = _attach(pipeline)
    helpers.drop(attached, resources=('droppable_a', 'droppable_b'))

    assert_dropped_resources(attached, ['droppable_a', 'droppable_b'])
    assert_destination_state_loaded(attached)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_resource_regex(destination_name: str) -> None:
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)

    helpers.drop(attached, resources=['re:.+_b', 're:.+_a'])

    attached = _attach(pipeline)

    assert_dropped_resources(attached, ['droppable_a', 'droppable_b'])
    assert_destination_state_loaded(attached)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_drop_nothing(destination_name: str) -> None:
    """No resources, no state keys. Nothing is changed."""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)

    attached = _attach(pipeline)
    previous_state = dict(attached.state)

    helpers.drop(attached)

    assert_dropped_resources(attached, [])
    assert previous_state == attached.state


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_drop_all_flag(destination_name: str) -> None:
    """Using drop_all flag. Destination dataset and all local state is deleted"""
    source = droppable_source()
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(source)
    dlt_tables = [t['name'] for t in pipeline.default_schema.dlt_tables()]  # Original _dlt tables to check for

    attached = _attach(pipeline)

    helpers.drop(attached, drop_all=True)

    attached = _attach(pipeline)

    assert_dropped_resources(attached, list(RESOURCE_TABLES))

    # Verify original _dlt tables were not deleted
    with attached._sql_job_client(attached.default_schema) as client:
        for tbl in dlt_tables:
            exists, _ = client.get_storage_table(tbl)
            assert exists


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_run_pipeline_after_partial_drop(destination_name: str) -> None:
    """Pipeline can be run again after dropping some resources"""
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(droppable_source())

    attached = _attach(pipeline)

    helpers.drop(attached, resources='droppable_a')

    attached = _attach(pipeline)

    attached.extract(droppable_source())  # TODO: individual steps cause pipeline.run() never raises
    attached.normalize()
    attached.load(raise_on_failed_jobs=True)


@pytest.mark.parametrize('destination_name', ALL_DESTINATIONS)
def test_drop_state_only(destination_name: str) -> None:
    """Pipeline can be run again after dropping some resources"""
    pipeline = dlt.pipeline(pipeline_name='drop_test_' + uniq_id(), destination=destination_name)
    pipeline.run(droppable_source())

    attached = _attach(pipeline)

    helpers.drop(attached, resources=('droppable_a', 'droppable_b'), state_only=True)

    attached = _attach(pipeline)

    assert_dropped_resource_tables(attached, [])  # No tables dropped
    assert_dropped_resource_states(attached, ['droppable_a', 'droppable_b'])
    assert_destination_state_loaded(attached)


if __name__ == '__main__':
    import pytest
    pytest.main(['-k', 'drop_all', 'tests/load/pipeline/test_drop.py', '--pdb', '-s'])
