from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from pipeline.executor import execute_node
from pipeline.models import PipelineState
from pipeline.planner import plan_node
from pipeline.renderer import render_node
from pipeline.reporter import report_node
from pipeline.reviser import reviser_node
from pipeline.spec_loader import load_spec_node
from pipeline.triage import increment_iteration, route_after_triage, triage_node
from pipeline.validator import route_after_validate, validate_plan


def validate_node(state: PipelineState) -> dict[str, object]:
    return validate_plan(state)


def route_after_triage_with_increment(state: PipelineState) -> str:
    decision = route_after_triage(state)
    return decision


def build_graph():
    graph = StateGraph(PipelineState)

    graph.add_node("load_spec", load_spec_node)
    graph.add_node("plan", plan_node)
    graph.add_node("validate", validate_node)
    graph.add_node("render", render_node)
    graph.add_node("execute", execute_node)
    graph.add_node("triage", triage_node)
    graph.add_node("revise", reviser_node)
    graph.add_node("report", report_node)
    graph.add_node("bump_iteration", increment_iteration)

    graph.set_entry_point("load_spec")
    graph.add_edge("load_spec", "plan")
    graph.add_edge("plan", "validate")
    graph.add_conditional_edges(
        "validate",
        route_after_validate,
        {"retry_plan": "plan", "render": "render"},
    )
    graph.add_edge("render", "execute")
    graph.add_edge("execute", "triage")

    def triage_router(state: PipelineState) -> str:
        if route_after_triage(state) == "revise":
            return "bump_iteration"
        return "report"

    graph.add_conditional_edges(
        "triage",
        triage_router,
        {"bump_iteration": "bump_iteration", "report": "report"},
    )
    graph.add_edge("bump_iteration", "revise")
    graph.add_edge("revise", "validate")
    graph.add_edge("report", END)

    memory = MemorySaver()
    return graph.compile(checkpointer=memory)


def run_pipeline(initial: PipelineState) -> PipelineState:
    app = build_graph()
    config = {"configurable": {"thread_id": "pipeline-run"}}
    final_state = None
    for event in app.stream(initial, config=config, stream_mode="values"):
        final_state = event
    if final_state is None:
        return initial
    if isinstance(final_state, PipelineState):
        return final_state
    return PipelineState.model_validate(final_state)
