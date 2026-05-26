

## 1、Tool Policy设计

| 当前状态                     | 可暴露工具                  |
| :--------------------------- | :-------------------------- |
| 刚开始                       | search_metrics              |
| metrics 成功                 | search_schema               |
| schema 成功且有允许表        | search_schema, validate_sql |
| SQL 校验失败且仍有预算       | search_schema, validate_sql |
| SQL 校验成功，execute=False  | 无工具，允许结束            |
| SQL 校验成功，execute=True   | execute_sql                 |
| SQL 执行成功，包括返回空列表 | explain_result              |
| explanation 成功             | 无工具，允许结束            |



```mermaid
---
config:
  layout: elk
---
graph TD
    Start([刚开始])
    Start --> T1[[search_metrics]]
    T1 --> S1(metrics成功)
    S1 --> T2[[search_schema]]
    T2 --> S2(schema成功且有允许表)
    S2 --> T3[[search_schema]]
    S2 --> T4[[validate_sql]]
    T3 --> S2
    T4 --> Cond{校验结果与参数}
    Cond -->|失败且仍有预算| S3(SQL校验失败且仍有预算)
    S3 --> T3
    S3 --> T4
    Cond -->|成功且execute=False| S4(SQL校验成功execute=False)
    S4 --> End1([允许结束])
    Cond -->|成功且execute=True| S5(SQL校验成功execute=True)
    S5 --> T5[[execute_sql]]
    T5 --> S6(SQL执行成功包括返回空列表)
    S6 --> T6[[explain_result]]
    T6 --> S7(explanation成功)
    S7 --> End2([允许结束])
    
    classDef state fill:#f9f9f9,stroke:#333,stroke-width:2px
    classDef tool fill:#e1f5fe,stroke:#0288d1,stroke-width:2px,stroke-dasharray:5 5
    classDef endState fill:#e8f5e9,stroke:#4caf50,stroke-width:2px
    
    class Start,S1,S2,Cond,S3,S4,S5,S6,S7 state
    class T1,T2,T3,T4,T5,T6 tool
    class End1,End2 endState
```

