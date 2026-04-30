from engine.metrics import Metric

class EmbeddingDocument:
    def __init__(self, metric:Metric):
        self._metric = metric
    
    def build_content(self)->str:
        content='''
Metric: {metric_name}
Display name: {metric_display_name}
Business definition: {metric_def}
SQL expression: {metric_sqlexpr}
Base table: {metric_base_table}
Join tables: {metric_join_tables}
Time column: {metric_time_column}
Dimensions: {metric_dimensions}
Default filters: {metric_filters}
Forbidden conditions: {metric_forbidden}
Synonyms: {metric_synonyms}
        '''
        return content.format(metric_name = self._metric.name,
                              metric_display_name = self._metric.display_name,
                              metric_def = self._metric.business_def,
                              metric_sqlexpr = self._metric.sql_expr,
                              metric_base_table = self._metric.base_table,
                              metric_join_tables = self._metric.join_tables,
                              metric_time_column = self._metric.time_column,
                              metric_dimensions = self._metric.dimensions,
                              metric_filters = self._metric.filters,
                              metric_forbidden = self._metric.forbidden,
                              metric_synonyms = self._metric.synonyms
                              )
