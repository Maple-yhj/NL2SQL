from langgraph.graph import StateGraph, END, START
from graph.state import InputState,OutputState,GraphState
from graph.node import InputNode,ParseIntentNode,SearchIntentNode,GenerateSQLNode,ValidateSQLNode,ExecuteSQLNode,ExplainSQLNode


def route_after_validate(state:GraphState) -> str:
    validated_sql = state.get("validated_sql")
    if validated_sql and len(validated_sql) > 0:
        if state.get("execute", True):
            return "ExecuteSQLNode"
        else:
            return "END"
    else:
        attempts = state.get("validation_attempts", 0)
        # 检查是否超过最大重试次数
        if attempts >= 3:
            return "END"  
        else:
            return "GenerateSQLNode"
    


builder = StateGraph(GraphState)

builder.add_node("InputNode",InputNode)
builder.add_node("ParseIntentNode",ParseIntentNode)
builder.add_node("SearchIntentNode",SearchIntentNode)
builder.add_node("GenerateSQLNode",GenerateSQLNode)
builder.add_node("ValidateSQLNode",ValidateSQLNode)
builder.add_node("ExecuteSQLNode",ExecuteSQLNode)
builder.add_node("ExplainSQLNode",ExplainSQLNode)

builder.set_entry_point("InputNode")
builder.add_edge("InputNode","ParseIntentNode")
builder.add_edge("ParseIntentNode","SearchIntentNode")
builder.add_edge("SearchIntentNode","GenerateSQLNode")
builder.add_edge("GenerateSQLNode","ValidateSQLNode")

builder.add_conditional_edges(
    "ValidateSQLNode",
    route_after_validate,
    ["ExecuteSQLNode","GenerateSQLNode",END]
)

builder.add_edge("ExecuteSQLNode","ExplainSQLNode")
builder.add_edge("ExplainSQLNode",END)