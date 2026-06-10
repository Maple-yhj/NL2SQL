from langgraph.graph import StateGraph
from graph.tools import InputState,ParseState,SearchIntentState,GenSQLState,ValidateSQLState,ExecuteSQLState


def InputNode(state: InputState):
    ...

def ParseIntentNode(state: InputState) -> ParseState:
    ...

def SearchIntentNode(state: ParseState) -> SearchIntentState:
    ...

def GenerateSQLNode(state: SearchIntentState) -> GenSQLState:
    ...

def ValidateSQLNode(state: GenSQLState) -> ValidateSQLState:
    ...

def ExecuteSQLNode(state: ValidateSQLState) -> ExecuteSQLState:
    ...

