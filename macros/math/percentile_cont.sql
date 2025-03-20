{% macro percentile_cont(field, quantile, partition=None) %}
  {{ adapter.dispatch('quantile', 'dbt_expectations') (field, quantile, partition) }}
{% endmacro %}

{% macro default__quantile(field, quantile, partition)  -%}
    percentile_cont({{ quantile }}) within group (order by {{ field }})
    {%- if partition %}over(partition by {{ partition }}){% endif -%}
{%- endmacro %}

{% macro bigquery__quantile(field, quantile, partition) -%}
    percentile_cont({{ field }}, {{ quantile }})
    over({%- if partition %}partition by {{ partition }}{% endif -%})
{% endmacro %}

{% macro spark__quantile(field, quantile, partition) -%}
    percentile({{ field }}, {{ quantile }})
    over({%- if partition %}partition by {{ partition }}{% endif -%})
{% endmacro %}

{% macro maxcompute__quantile(field, quantile, partition) -%}
    {%- if partition != None %}
    {{ exceptions.warn(
            "partition options are not supported for dbt-maxcompute"
            ~ "and are being ignored."
    ) }}
    {% endif %}
    percentile({{ field }}, {{ quantile }})
{% endmacro %}
