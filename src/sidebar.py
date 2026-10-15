"""Reconstruct the local Codex sidebar from its saved database and sidebar state."""
import json
from pathlib import Path
import re


def object_value(value):
    if isinstance(value, dict): return value
    try:
        parsed = json.loads(value or '{}')
        return parsed if isinstance(parsed, dict) else {}
    except (ValueError, TypeError): return {}


def key_path(value):
    return str(value or '').replace('/', '\\').rstrip('\\').casefold()


def apply(chats, rows, projects, roots, sections, global_state, home):
    atom = object_value(global_state.get('electron-persisted-atom-state'))
    prefs = object_value(atom.get('flat-project-sidebar-preferences-v1'))
    legacy = object_value(global_state.get('local-projects'))
    projects = {**{k: dict(v) for k, v in projects.items()}, **{k: v for k, v in legacy.items() if isinstance(v, dict)}}
    aliases = {}
    maps = object_value(global_state.get('app-server-project-id-by-legacy-project-id-by-host'))
    for host, mapping in maps.items():
        if key_path(host) == key_path('local:' + str(home)) and isinstance(mapping, dict):
            aliases.update({new: old for old, new in mapping.items()})
    root_paths = {}
    for root in roots:
        root_paths.setdefault(aliases.get(root['project_id'], root['project_id']), []).append(root.get('path'))
    for pid, project in projects.items():
        root_paths.setdefault(pid, []).extend(project.get('rootPaths', []))
    assignments = object_value(global_state.get('thread-project-assignments'))
    projectless = set(global_state.get('projectless-thread-ids', []))
    by_id = {r['id']: r for r in chats}; records = {r['id']: r for r in rows}
    for row in chats:
        record = records[row['id']]
        pid = aliases.get(record.get('project_id'), record.get('project_id')) or ''
        assignment = assignments.get(row['id'])
        if not pid and isinstance(assignment, dict) and assignment.get('projectKind') == 'local':
            pid = assignment.get('projectId') or ''
        if not pid and row['id'] not in projectless:
            cwd = key_path(record.get('cwd'))
            matches = [(len(key_path(p)), project) for project, paths in root_paths.items() for p in paths
                       if p and (cwd == key_path(p) or cwd.startswith(key_path(p) + '\\'))]
            if matches: pid = max(matches)[1]
        row['project_id'] = pid
        row['project'] = projects.get(pid, {}).get('name') or ('项目 · ' + pid[:8] if pid else '聊天')
        source = object_value(record.get('source'))
        row['helper'] = bool(row.get('helper') or source.get('subagent') or
                             record.get('thread_source') in ('guardian_review', 'subagent') or record.get('agent_nickname'))
        row['guardian'] = bool(record.get('thread_source') == 'guardian_review' or
                               object_value(source.get('subagent')).get('other') == 'guardian' or
                               (row['helper'] and row['title'].casefold() == 'guardian review'))
        row['section_id'] = record.get('thread_section_id') or ''
        row['section_position'] = record.get('section_position') or 0
        row['updated_at'] = record.get('recency_at_ms') or record.get('updated_at_ms') or \
                            int(record.get('recency_at') or record.get('updated_at') or 0) * 1000
    for row in chats:
        ancestor, seen = row, {row['id']}
        while not row['project_id'] and ancestor['parent_id'] in by_id:
            ancestor = by_id[ancestor['parent_id']]
            if ancestor['id'] in seen: break
            seen.add(ancestor['id'])
            if ancestor['project_id']:
                row['project_id'], row['project'] = ancestor['project_id'], ancestor['project']
        parent = by_id.get(row['parent_id'], {})
        row['parent_title'] = parent.get('original_title') or parent.get('title') or ''
        row['original_title'] = row['title']
        if row['guardian']:
            row['title'] = '安全审查' + (' · ' + row['parent_title'] if row['parent_title'] else '')
        elif row['helper']:
            row['title'] = '子助手 · ' + row['title']
    pinned_order = []
    for value in atom.get('unified-sidebar-pinned-order-v1', []) or []:
        if isinstance(value, str) and value.startswith('codex:thread:local:'):
            pinned_order.append(value.split(':')[-1])
    for sid in atom.get('app-server-pinned-thread-order-v1', []) or []:
        if sid not in pinned_order: pinned_order.append(sid)
    for sid in global_state.get('pinned-thread-ids', []):
        if sid not in pinned_order: pinned_order.append(sid)
    for record in rows:
        if record.get('is_pinned') and record['id'] not in pinned_order: pinned_order.append(record['id'])
    project_order = []
    for key in atom.get('unified-sidebar-project-order-v1', []) or []:
        if isinstance(key, str) and key.startswith('codex:project:'): project_order.append(key.split(':', 2)[-1])
    for pid in global_state.get('project-order', []):
        if pid not in project_order: project_order.append(pid)
    known = {r['project_id'] for r in chats if r['project_id']}
    known.update(aliases.get(pid, pid) for pid in projects)
    if prefs.get('projectSortMode') == 'updated_at':
        project_order = sorted(known, key=lambda pid: -max([r['updated_at'] for r in chats if r['project_id'] == pid and not r['helper']] or [0]))
    else:
        project_order.extend(pid for pid in sorted(known, key=lambda p: projects.get(p, {}).get('position', 0)) if pid not in project_order)
    manual = object_value(global_state.get('sidebar-project-thread-orders'))
    for row in chats:
        if row['archived']:
            row['group_key'], row['group_name'], row['group_rank'] = 'archived', '已归档', 9000
        elif row['id'] in pinned_order:
            row['group_key'], row['group_name'], row['group_rank'] = 'pinned', '置顶', 0
        elif row['section_id'] and row['section_id'] in sections:
            row['group_key'], row['group_name'], row['group_rank'] = 'section:' + row['section_id'], sections[row['section_id']]['name'], 50
        elif row['project_id']:
            row['group_key'], row['group_name'] = 'project:' + row['project_id'], row['project']
            row['group_rank'] = 100 + (project_order.index(row['project_id']) if row['project_id'] in project_order else len(project_order))
        else:
            row['group_key'], row['group_name'], row['group_rank'] = 'chats', '聊天', 8000
        if row['group_key'] == 'pinned':
            row['sort_order'] = pinned_order.index(row['id'])
        elif row['section_id'] or prefs.get('chatSortMode') == 'manual':
            order = manual.get(row['project_id'], [])
            row['sort_order'] = order.index(row['id']) if row['id'] in order else row['section_position']
        else:
            row['sort_order'] = -row['updated_at']
    return sorted(chats, key=lambda r: (r['group_rank'], r['helper'], r['sort_order'], r['id']))
