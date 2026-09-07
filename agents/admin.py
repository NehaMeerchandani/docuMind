import json

from django.contrib import admin
from django.http import JsonResponse
from django.shortcuts import render
from django.urls import path
from django.views.decorators.http import require_POST
from unfold.admin import ModelAdmin

from agents.graph.builder import WorkflowGraphBuilder, WorkflowValidationError
from agents.models import Agent
from agents.services.tool_registry import ToolRegistry
from base.admin import AuditableAdminMixin


def workflow_editor_view(request):
    context = {
        **admin.site.each_context(request),
        'tool_choices_json': json.dumps(ToolRegistry.choices()),
    }
    return render(request, 'admin/agents/workflow_editor.html', context)


def agent_list_view(request):
    agents = list(Agent.objects.all().values('id', 'name', 'description'))
    return JsonResponse({'success': True, 'error': None, 'data': agents})


def agent_detail_view(request, pk):
    try:
        agent = Agent.objects.get(pk=pk)
    except Agent.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Agent not found.', 'data': None}, status=404)

    return JsonResponse({
        'success': True,
        'error': None,
        'data': {
            'id': agent.id,
            'name': agent.name,
            'description': agent.description,
            'workflow_json': agent.workflow_json,
        },
    })


@require_POST
def agent_save_view(request):
    """Creates a new agent, or updates an existing one if `id` is present in the body.

    Validates the submitted workflow_json through the exact same
    WorkflowGraphBuilder._parse_and_validate the chat runtime itself uses, so a
    workflow that fails to save here is guaranteed to also be one that couldn't have
    run in chat anyway -- there's no separate, looser validation path.
    """
    try:
        body = json.loads(request.body or b'{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON body.', 'data': None}, status=400)

    name = (body.get('name') or '').strip()
    description = body.get('description') or ''
    workflow_json = body.get('workflow_json') or {}

    if not name:
        return JsonResponse({'success': False, 'error': 'name is required.', 'data': None}, status=400)

    try:
        WorkflowGraphBuilder.validate(workflow_json)
    except WorkflowValidationError as exc:
        return JsonResponse({'success': False, 'error': str(exc), 'data': None}, status=400)

    agent_id = body.get('id')
    if agent_id:
        try:
            agent = Agent.objects.get(pk=agent_id)
        except Agent.DoesNotExist:
            return JsonResponse({'success': False, 'error': 'Agent not found.', 'data': None}, status=404)
        agent.name = name
        agent.description = description
        agent.workflow_json = workflow_json
        agent.updated_by = request.user
        agent.save(update_fields=['name', 'description', 'workflow_json', 'updated_by'])
    else:
        if Agent.objects.filter(name=name).exists():
            return JsonResponse(
                {'success': False, 'error': f'An agent named "{name}" already exists.', 'data': None},
                status=400,
            )
        agent = Agent.objects.create(
            name=name, description=description, workflow_json=workflow_json,
            created_by=request.user, updated_by=request.user,
        )

    return JsonResponse({'success': True, 'error': None, 'data': {'id': agent.id}})


@admin.register(Agent)
class AgentAdmin(AuditableAdminMixin, ModelAdmin):
    list_display = ['id', 'name', 'description', 'created_at']
    search_fields = ['name', 'description']

    fieldsets = (
        ('Details', {
            'classes': ('tab',),
            'fields': ('name', 'description', 'workflow_json', 'is_active'),
        }),
        ('Audit info', {
            'classes': ('tab',),
            'fields': (
                'created_by', 'updated_by', 'created_at', 'updated_at',
                'is_deleted', 'deleted_at',
            ),
        }),
    )
    readonly_fields = ['created_at', 'updated_at']

    def get_urls(self):
        custom_urls = [
            path(
                'workflow-editor/',
                self.admin_site.admin_view(workflow_editor_view),
                name='agents_workflow_editor',
            ),
            path(
                'workflow-editor/agents/',
                self.admin_site.admin_view(agent_list_view),
                name='agents_workflow_editor_list',
            ),
            path(
                'workflow-editor/agents/<int:pk>/',
                self.admin_site.admin_view(agent_detail_view),
                name='agents_workflow_editor_detail',
            ),
            path(
                'workflow-editor/save/',
                self.admin_site.admin_view(agent_save_view),
                name='agents_workflow_editor_save',
            ),
        ]
        return custom_urls + super().get_urls()
