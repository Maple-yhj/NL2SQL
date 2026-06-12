from langgraph.graph import StateGraph
from graph.tools import InputState,OutputState,GraphState


def InputNode(state: InputState) -> GraphState:
    ...

def ParseIntentNode(state: GraphState) -> GraphState:
    ...

def SearchIntentNode(state: GraphState) -> GraphState:
    ...

def GenerateSQLNode(state: GraphState) -> GraphState:
    ...

def ValidateSQLNode(state: GraphState) -> GraphState:
    ...

def ExecuteSQLNode(state: GraphState) -> GraphState:
    ...

def ExplainSQLNode(state: GraphState) -> OutputState:
    ...
