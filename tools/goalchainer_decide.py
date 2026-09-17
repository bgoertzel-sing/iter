"""Run the GoalChainer decision pipeline with WMTM active set as evidence.

GoalChainer is a goal-aware decision layer that:
1. Parses the request into evidence signals
2. Derives deontic status (forbidden/obligated/permitted)
3. Grades action acceptability via PLN-style evidence
4. Applies subjective-logic verdicts
5. Reconciles individual and collective goal pressures
6. Exposes the selected action as a directive

When WMTM is active, its current working set is fed as memory_items
to adjust beliefs based on promoted memory evidence.
"""
import importlib.util, json, os, sys
from pathlib import Path

DESCRIPTION = "Run the GoalChainer goal-aware decision pipeline. Arg: request (str) describing the decision scenario. Returns ranked actions with deontic status and motivation."

def _build_memory_items():
    """Build memory_items from WMTM by initializing from the journal."""
    try:
        # Add wmtm package to path
        wmtm_dir = Path(__file__).resolve().parent.parent / 'wmtm'
        if str(wmtm_dir) not in sys.path:
            sys.path.insert(0, str(wmtm_dir))
        
        from wmtm import WMTMStore, WMTMOrchestrator
        from wmtm.recall_bridge import parse_journal, RecallBridge
        
        # Load journal
        j_path = Path(__file__).resolve().parent.parent / '_petta_journal.py'
        spec = importlib.util.spec_from_file_location('_petta_journal', j_path)
        j = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(j)
        
        lines = j._read_lines()
        resolved = j.resolve_supersedes(lines)
        clusters = parse_journal(resolved)
        
        # Create store and recall bridge
        store = WMTMStore(capacity=60)
        rb = RecallBridge(clusters)
        
        # Recall a few items based on the request (will be passed as run arg)
        # For now, just admit the most recent clusters
        for c in clusters[-15:]:
            store.admit(c.id, c.event_note or c.text, source_type='recalled', initial_sti=10.0)
        
        # Run a cycle for inference
        orch = WMTMOrchestrator(store)
        orch.cycle()
        
        # Convert to GoalChainer format
        items = []
        for item in store.get_active_set()[:20]:
            items.append({
                'id': item.id,
                'content': item.content,
                'sti': float(item.attention.sti),
            })
        return items if items else None
    except Exception:
        return None

def run(request):
    """Run GoalChainer decision pipeline."""
    try:
        # Add GoalChainer to path
        gc_dir = '/home/openclaw/research-agent/projects/omegaclaw/repos/OmegaClaw-GoalChainer/src'
        if gc_dir not in sys.path:
            sys.path.insert(0, gc_dir)
        
        # Use heuristic PLN (avoids PeTTaChainer stack overflow)
        os.environ.setdefault('GOALCHAINER_USE_HEURISTIC_PLN', '1')
        
        from goal_chainer.pipeline import solve_incident
        
        # Build memory items from WMTM
        memory_items = _build_memory_items()
        
        result = solve_incident(request, memory_items=memory_items)
        
        # Format result
        lines = []
        lines.append('Decision: ' + str(result.get('decided')) + ' - ' + str(result.get('label')))
        lines.append('Status: ' + str(result.get('status')))
        lines.append('')
        lines.append('Ranked actions:')
        for d in result.get('decisions', []):
            score = d.get('score', 'N/A')
            lines.append('  ' + str(d.get('action_id')) + ': ' + str(d.get('label')) + ' - ' + str(d.get('status')) + ' (score=' + str(score) + ')')
        
        motivation = result.get('motivation')
        if motivation:
            lines.append('')
            lines.append('Motivation: ' + str(motivation)[:200])
        
        if memory_items:
            lines.append('')
            lines.append('WMTM evidence: ' + str(len(memory_items)) + ' items fed to GoalChainer')
        
        return '\n'.join(lines)
    except Exception as e:
        return 'GoalChainer error: ' + str(e)
