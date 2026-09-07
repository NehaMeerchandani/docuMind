from django.db import models

from base.models import BaseModel


class Agent(BaseModel):
    """An admin-authored, dynamically-executed chat workflow.

    `name` is the agent's own identifier and is shown to the router (see
    agents/services/router_service.py) so the LLM can pick which agent applies to an
    incoming message. It also doubles as the fallback if a workflow has just one STEP --
    see workflow_json's node-level `name`, which is the actual Langfuse prompt slug for
    that step.

    `workflow_json` stores the graph authored in the visual editor (agents/graph/), e.g.:
        {
          "nodes": [
            {"id": "n1", "type": "start", "position": {"x": 40, "y": 40}},
            {
              "id": "n2", "type": "step", "position": {"x": 320, "y": 40},
              "data": {"name": "rag-system-prompt", "tools": ["search_documents", "summarize_session"]}
            },
            {"id": "n3", "type": "end", "position": {"x": 600, "y": 40}}
          ],
          "edges": [
            {"id": "e1", "source": "n1", "target": "n2"},
            {
              "id": "e2", "source": "n2", "target": "n3",
              "data": {"condition": "always"}
              # condition is one of: "always", "tool_called:<tool_name>", "tool_not_called:<tool_name>"
            }
          ]
        }
    This shape is intentionally close to what a React-Flow-style editor natively
    produces (nodes with id/type/position/data, edges with id/source/target/data), even
    though our editor is hand-built rather than React, so the JSON stays portable if the
    editor is ever swapped out.
    """

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(
        blank=True,
        help_text=(
            "Shown to the router LLM when deciding which agent should handle an incoming "
            "message -- describe what this agent is for and when it should be picked."
        ),
    )
    workflow_json = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return self.name
