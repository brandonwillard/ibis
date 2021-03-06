import collections

from datetime import date, datetime

import pytest

import numpy as np
import pandas as pd
import pandas.util.testing as tm

import ibis
import ibis.common as com
import ibis.expr.datatypes as dt
import ibis.expr.types as ir


pytestmark = pytest.mark.bigquery
pytest.importorskip('google.cloud.bigquery')
exceptions = pytest.importorskip('google.api_core.exceptions')


def test_table(alltypes):
    assert isinstance(alltypes, ir.TableExpr)


def test_column_execute(alltypes, df):
    col_name = 'float_col'
    expr = alltypes[col_name]
    result = expr.execute()
    expected = df[col_name]
    tm.assert_series_equal(result, expected)


def test_literal_execute(client):
    expected = '1234'
    expr = ibis.literal(expected)
    result = client.execute(expr)
    assert result == expected


def test_simple_aggregate_execute(alltypes, df):
    col_name = 'float_col'
    expr = alltypes[col_name].sum()
    result = expr.execute()
    expected = df[col_name].sum()
    np.testing.assert_allclose(result, expected)


def test_list_tables(client):
    tables = client.list_tables(like='functional_alltypes')
    assert set(tables) == {
        'functional_alltypes',
        'functional_alltypes_parted',
    }


def test_current_database(client):
    assert client.current_database.name == 'testing'
    assert client.current_database.name == client.dataset_id
    assert client.current_database.tables == client.list_tables()


def test_database(client):
    database = client.database(client.dataset_id)
    assert database.list_tables() == client.list_tables()


def test_compile_toplevel():
    t = ibis.table([('foo', 'double')], name='t0')

    # it works!
    expr = t.foo.sum()
    result = ibis.bigquery.compile(expr)
    # FIXME: remove quotes because bigquery can't use anythig that needs
    # quoting?
    expected = """\
SELECT sum(`foo`) AS `sum`
FROM t0"""  # noqa
    assert str(result) == expected


def test_struct_field_access(struct_table):
    expr = struct_table.struct_col.string_field
    result = expr.execute()
    expected = pd.Series([None, 'a'], name='tmp')
    tm.assert_series_equal(result, expected)


def test_array_index(struct_table):
    expr = struct_table.array_of_structs_col[1]
    result = expr.execute()
    expected = pd.Series(
        [
            {'int_field': None, 'string_field': None},
            {'int_field': None, 'string_field': 'hijklmnop'}
        ],
        name='tmp'
    )
    tm.assert_series_equal(result, expected)


def test_array_concat(struct_table):
    c = struct_table.array_of_structs_col
    expr = c + c
    result = expr.execute()
    expected = pd.Series(
        [
            [
                {'int_field': 12345, 'string_field': 'abcdefg'},
                {'int_field': None, 'string_field': None},
                {'int_field': 12345, 'string_field': 'abcdefg'},
                {'int_field': None, 'string_field': None},
            ],
            [
                {'int_field': 12345, 'string_field': 'abcdefg'},
                {'int_field': None, 'string_field': 'hijklmnop'},
                {'int_field': 12345, 'string_field': 'abcdefg'},
                {'int_field': None, 'string_field': 'hijklmnop'},
            ],
        ],
        name='tmp',
    )
    tm.assert_series_equal(result, expected)


def test_array_length(struct_table):
    expr = struct_table.array_of_structs_col.length()
    result = expr.execute()
    expected = pd.Series([2, 2], name='tmp')
    tm.assert_series_equal(result, expected)


def test_array_collect(struct_table):
    key = struct_table.array_of_structs_col[0].string_field
    expr = struct_table.groupby(key=key).aggregate(
        foo=lambda t: t.array_of_structs_col[0].int_field.collect()
    )
    result = expr.execute()
    expected = struct_table.execute()
    expected = expected.assign(
        key=expected.array_of_structs_col.apply(lambda x: x[0]['string_field'])
    ).groupby('key').apply(
        lambda df: list(
            df.array_of_structs_col.apply(lambda x: x[0]['int_field'])
        )
    ).reset_index().rename(columns={0: 'foo'})
    tm.assert_frame_equal(result, expected)


def test_count_distinct_with_filter(alltypes):
    expr = alltypes.string_col.nunique(
        where=alltypes.string_col.cast('int64') > 1
    )
    result = expr.execute()
    expected = alltypes.string_col.execute()
    expected = expected[expected.astype('int64') > 1].nunique()
    assert result == expected


@pytest.mark.parametrize('type', ['date', dt.date])
def test_cast_string_to_date(alltypes, df, type):
    import toolz

    string_col = alltypes.date_string_col
    month, day, year = toolz.take(3, string_col.split('/'))

    expr = '20' + ibis.literal('-').join([year, month, day])
    expr = expr.cast(type)

    result = expr.execute().astype(
        'datetime64[ns]'
    ).sort_values().reset_index(drop=True).rename('date_string_col')
    expected = pd.to_datetime(
        df.date_string_col
    ).dt.normalize().sort_values().reset_index(drop=True)
    tm.assert_series_equal(result, expected)


def test_has_partitions(alltypes, parted_alltypes, client):
    col = ibis.options.bigquery.partition_col
    assert col not in alltypes.columns
    assert col in parted_alltypes.columns


def test_different_partition_col_name(client):
    col = ibis.options.bigquery.partition_col = 'FOO_BAR'
    alltypes = client.table('functional_alltypes')
    parted_alltypes = client.table('functional_alltypes_parted')
    assert col not in alltypes.columns
    assert col in parted_alltypes.columns


def test_subquery_scalar_params(alltypes):
    t = alltypes
    param = ibis.param('timestamp').name('my_param')
    expr = t[['float_col', 'timestamp_col', 'int_col', 'string_col']][
        lambda t: t.timestamp_col < param
    ].groupby('string_col').aggregate(
        foo=lambda t: t.float_col.sum()
    ).foo.count()
    result = expr.compile(params={param: '20140101'})
    expected = """\
SELECT count(`foo`) AS `count`
FROM (
  SELECT `string_col`, sum(`float_col`) AS `foo`
  FROM (
    SELECT `float_col`, `timestamp_col`, `int_col`, `string_col`
    FROM `ibis-gbq.testing.functional_alltypes`
    WHERE `timestamp_col` < @my_param
  ) t1
  GROUP BY 1
) t0"""
    assert result == expected


_IBIS_TYPE_TO_DTYPE = {
    'string': 'STRING',
    'int64': 'INT64',
    'double': 'FLOAT64',
    'boolean': 'BOOL',
    'timestamp': 'TIMESTAMP',
    'date': 'DATE',
}


def test_scalar_param_string(alltypes, df):
    param = ibis.param('string')
    expr = alltypes[alltypes.string_col == param]

    string_value = '0'
    result = expr.execute(
        params={param: string_value}
    ).sort_values('id').reset_index(drop=True)
    expected = df.loc[
        df.string_col == string_value
    ].sort_values('id').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


def test_scalar_param_int64(alltypes, df):
    param = ibis.param('int64')
    expr = alltypes[alltypes.string_col.cast('int64') == param]

    int64_value = 0
    result = expr.execute(
        params={param: int64_value}
    ).sort_values('id').reset_index(drop=True)
    expected = df.loc[
        df.string_col.astype('int64') == int64_value
    ].sort_values('id').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


def test_scalar_param_double(alltypes, df):
    param = ibis.param('double')
    expr = alltypes[alltypes.string_col.cast('int64').cast('double') == param]

    double_value = 0.0
    result = expr.execute(
        params={param: double_value}
    ).sort_values('id').reset_index(drop=True)
    expected = df.loc[
        df.string_col.astype('int64').astype('float64') == double_value
    ].sort_values('id').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


def test_scalar_param_boolean(alltypes, df):
    param = ibis.param('boolean')
    expr = alltypes[(alltypes.string_col.cast('int64') == 0) == param]

    bool_value = True
    result = expr.execute(
        params={param: bool_value}
    ).sort_values('id').reset_index(drop=True)
    expected = df.loc[
        df.string_col.astype('int64') == 0
    ].sort_values('id').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    'timestamp_value',
    ['2009-01-20 01:02:03', date(2009, 1, 20), datetime(2009, 1, 20, 1, 2, 3)]
)
def test_scalar_param_timestamp(alltypes, df, timestamp_value):
    param = ibis.param('timestamp')
    expr = alltypes[alltypes.timestamp_col <= param][['timestamp_col']]

    result = expr.execute(
        params={param: timestamp_value}
    ).sort_values('timestamp_col').reset_index(drop=True)
    value = pd.Timestamp(timestamp_value, tz='UTC')
    expected = df.loc[
        df.timestamp_col <= value, ['timestamp_col']
    ].sort_values('timestamp_col').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


@pytest.mark.parametrize(
    'date_value',
    ['2009-01-20', date(2009, 1, 20), datetime(2009, 1, 20)]
)
def test_scalar_param_date(alltypes, df, date_value):
    param = ibis.param('date')
    expr = alltypes[alltypes.timestamp_col.cast('date') <= param]

    result = expr.execute(
        params={param: date_value}
    ).sort_values('timestamp_col').reset_index(drop=True)
    value = pd.Timestamp(date_value)
    expected = df.loc[
        df.timestamp_col.dt.normalize() <= value
    ].sort_values('timestamp_col').reset_index(drop=True)
    tm.assert_frame_equal(result, expected)


def test_scalar_param_array(alltypes, df):
    param = ibis.param('array<double>')
    expr = alltypes.sort_by('id').limit(1).double_col.collect() + param
    result = expr.execute(params={param: [1]})
    expected = [df.sort_values('id').double_col.iat[0]] + [1.0]
    assert result == expected


def test_scalar_param_struct(client):
    struct_type = dt.Struct.from_tuples([('x', dt.int64), ('y', dt.string)])
    param = ibis.param(struct_type)
    value = collections.OrderedDict([('x', 1), ('y', 'foobar')])
    result = client.execute(param, {param: value})
    assert value == result


@pytest.mark.xfail(
    raises=com.UnsupportedBackendType,
    reason='Cannot handle nested structs/arrays in 0.27 API',
)
def test_scalar_param_nested(client):
    param = ibis.param('struct<x: array<struct<y: array<double>>>>')
    value = collections.OrderedDict([
        (
            'x',
            [
                collections.OrderedDict([
                    ('y', [1.0, 2.0, 3.0])
                ])
            ]
        )
    ])
    result = client.execute(param, {param: value})
    assert value == result


def test_raw_sql(client):
    assert client.raw_sql('SELECT 1').fetchall() == [(1,)]


def test_scalar_param_scope(alltypes):
    t = alltypes
    param = ibis.param('timestamp')
    mut = t.mutate(param=param).compile(params={param: '2017-01-01'})
    assert mut == """\
SELECT *, @param AS `param`
FROM `ibis-gbq.testing.functional_alltypes`"""


def test_scalar_param_partition_time(parted_alltypes):
    t = parted_alltypes
    param = ibis.param('timestamp').name('time_param')
    expr = t[t.PARTITIONTIME < param]
    df = expr.execute(params={param: '2017-01-01'})
    assert df.empty


def test_exists_table(client):
    assert client.exists_table('functional_alltypes')
    assert not client.exists_table('footable')


def test_exists_database(client):
    assert client.exists_database('testing')
    assert not client.exists_database('foodataset')


@pytest.mark.parametrize('kind', ['date', 'timestamp'])
@pytest.mark.parametrize(
    ('option', 'expected_fn'),
    [
        (None, 'my_{}_parted_col'.format),
        ('PARTITIONTIME', lambda kind: 'PARTITIONTIME'),
        ('foo_bar', lambda kind: 'foo_bar'),
    ]
)
def test_parted_column(client, kind, option, expected_fn):
    table_name = '{}_column_parted'.format(kind)
    option_key = 'bigquery.partition_col'
    with ibis.config.option_context(option_key, option):
        t = client.table(table_name)
    expected_column = expected_fn(kind)
    assert t.columns == [expected_column, 'string_col', 'int_col']


def test_cross_project_query():
    con = ibis.bigquery.connect(
        project_id='ibis-gbq',
        dataset_id='bigquery-public-data.stackoverflow')
    table = con.table('posts_questions')
    expr = table[table.tags.contains('ibis')][['title', 'tags']]
    result = expr.compile()
    expected = """\
SELECT `title`, `tags`
FROM (
  SELECT *
  FROM `bigquery-public-data.stackoverflow.posts_questions`
  WHERE STRPOS(`tags`, 'ibis') - 1 >= 0
) t0"""
    assert result == expected
    n = 5
    df = expr.limit(n).execute()
    assert len(df) == n
    assert list(df.columns) == ['title', 'tags']
    assert df.title.dtype == np.object
    assert df.tags.dtype == np.object


def test_set_database():
    con = ibis.bigquery.connect(project_id='ibis-gbq', dataset_id='testing')
    con.set_database('bigquery-public-data.epa_historical_air_quality')
    tables = con.list_tables()
    assert 'co_daily_summary' in tables


def test_exists_table_different_project(client):
    name = 'co_daily_summary'
    database = 'bigquery-public-data.epa_historical_air_quality'
    assert client.exists_table(name, database=database)
    assert not client.exists_table('foobar', database=database)


def test_exists_table_different_project_fully_qualified(client):
    # TODO(phillipc): Should we raise instead?
    name = 'bigquery-public-data.epa_historical_air_quality.co_daily_summary'
    with pytest.raises(exceptions.BadRequest):
        client.exists_table(name)


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('bigquery-public-data.epa_historical_air_quality', True),
        ('bigquery-foo-bar-project.baz_dataset', False),
    ]
)
def test_exists_database_different_project(client, name, expected):
    assert client.exists_database(name) is expected


def test_repeated_project_name():
    con = ibis.bigquery.connect(
        project_id='ibis-gbq', dataset_id='ibis-gbq.testing')
    assert 'functional_alltypes' in con.list_tables()


@pytest.mark.xfail(raises=NotImplementedError, reason='async not implemented')
def test_async(client):
    expr = ibis.literal(1)
    result = client.execute(expr, async=True)
    assert result.get_result() == 1


def test_multiple_project_queries(client):
    so = client.table(
        'posts_questions', database='bigquery-public-data.stackoverflow')
    trips = client.table('trips', database='nyc-tlc.yellow')
    join = so.join(trips, so.tags == trips.rate_code)[[so.title]]
    result = join.compile()
    expected = """\
SELECT t0.`title`
FROM `bigquery-public-data.stackoverflow.posts_questions` t0
  INNER JOIN `nyc-tlc.yellow.trips` t1
    ON t0.`tags` = t1.`rate_code`"""
    assert result == expected


def test_multiple_project_queries_database_api(client):
    stackoverflow = client.database('bigquery-public-data.stackoverflow')
    posts_questions = stackoverflow.posts_questions
    yellow = client.database('nyc-tlc.yellow')
    trips = yellow.trips
    predicate = posts_questions.tags == trips.rate_code
    join = posts_questions.join(trips, predicate)[[posts_questions.title]]
    result = join.compile()
    expected = """\
SELECT t0.`title`
FROM `bigquery-public-data.stackoverflow.posts_questions` t0
  INNER JOIN `nyc-tlc.yellow.trips` t1
    ON t0.`tags` = t1.`rate_code`"""
    assert result == expected


def test_multiple_project_queries_execute(client):
    stackoverflow = client.database('bigquery-public-data.stackoverflow')
    posts_questions = stackoverflow.posts_questions.limit(5)
    yellow = client.database('nyc-tlc.yellow')
    trips = yellow.trips.limit(5)
    predicate = posts_questions.tags == trips.rate_code
    cols = [posts_questions.title]
    join = posts_questions.left_join(trips, predicate)[cols]
    result = join.execute()
    assert list(result.columns) == ['title']
    assert len(result) == 5
