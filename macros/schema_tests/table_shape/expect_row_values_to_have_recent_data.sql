{% test expect_row_values_to_have_recent_data(model,
                                                column_name,
                                                datepart,
                                                interval,
                                                row_condition=None) %}

 {{ adapter.dispatch('test_expect_row_values_to_have_recent_data', 'dbt_expectations') (model,
                                                                                        column_name,
                                                                                        datepart,
                                                                                        interval,
                                                                                        row_condition) }}

{% endtest %}

{% macro default__test_expect_row_values_to_have_recent_data(model, column_name, datepart, interval, row_condition) %}
{%- set default_start_date = '1970-01-01' -%}
{#
  MaxCompute 上 `cast('<仅日期串>' as timestamp)` 不报错、返回 NULL（服务端实测：
  `cast('1970-01-01' as timestamp)` → NULL，`cast('1970-01-01 00:00:00' as timestamp)` → 1970-01-01 00:00:00，
  `cast(cast('1970-01-01' as date) as timestamp)` → 1970-01-01 00:00:00）。
  这条保险丝因此恒为 NULL：模型为空、或 row_condition 过滤掉全部行时，coalesce 拿不到兜底日期，
  `NULL < <cutoff>` 恒为 NULL，末段 select 返回 0 行 → 测试判通过 —— 正是上面注释说要防的漏报。
  先按 timestamp 解析（接受完整时间串的仓库行为不变），落空时退回 date → timestamp 补齐 00:00:00。
#}
{%- set default_start_timestamp = "coalesce(cast('" ~ default_start_date ~ "' as " ~ dbt_expectations.type_timestamp() ~ "), cast(cast('" ~ default_start_date ~ "' as date) as " ~ dbt_expectations.type_timestamp() ~ "))" -%}
with max_recency as (

    select max(cast({{ column_name }} as {{ dbt_expectations.type_timestamp() }})) as max_timestamp
    from
        {{ model }}
    where
        -- to exclude erroneous future dates
        cast({{ column_name }} as {{ dbt_expectations.type_timestamp() }}) <= {{ dbt_date.now() }}
        {% if row_condition %}
        and {{ row_condition }}
        {% endif %}
)
select
    *
from
    max_recency
where
    -- if the row_condition excludes all rows, we need to compare against a default date
    -- to avoid false negatives
    coalesce(max_timestamp, {{ default_start_timestamp }})
        <
        cast({{ dbt.dateadd(datepart, interval * -1, dbt_date.now()) }} as {{ dbt_expectations.type_timestamp() }})

{% endmacro %}
