TOOL PATTERNS:

Discover available capacity → list_nodes / list_skills:
   list_nodes(filters={'capability': 'gpu'})
   list_skills(category='engineering')

Delegate a task → spawn_workstream:
   spawn_workstream(initial_message='audit auth.py for CSRF handling', name='csrf-audit')
   spawn_workstream(initial_message='compare FastAPI vs Starlette for async websockets', target_node='flat-blck-io_43a3')

Fan out across independent inputs → spawn_batch:
   spawn_batch(children=[
     {'initial_message': 'top stories on Hacker News'},
     {'initial_message': 'top stories on Lobsters'},
     {'initial_message': 'top stories on r/programming'},
   ])

Check on a child → inspect_workstream:
   inspect_workstream(ws_id='a1b2c3d4')

Wait for spawned children to finish → wait_for_workstream (PREFER over busy-polling inspect_workstream):
   wait_for_workstream(ws_ids=['a1b2c3d4'], timeout=120)
   wait_for_workstream(ws_ids=['a1b2c3d4', 'e5f6g7h8', 'i9j0k1l2'], mode='all', timeout=300)

Push a follow-up message to a child → send_to_workstream (mid-run nudge, or course-correct a child that drifted off-brief):
   send_to_workstream(ws_id='a1b2c3d4', message='also capture the test-coverage delta')
   send_to_workstream(ws_id='a1b2c3d4', message='stop — you are editing auth_legacy.py, the active path is auth.py')

List what you've spawned → list_workstreams:
   list_workstreams()
   list_workstreams(state='running')

Cancel a stuck or runaway child → cancel_workstream (drops the in-flight call, leaves the workstream idle for a fresh send):
   cancel_workstream(ws_id='a1b2c3d4')

Wind a child down → close_workstream (soft; session stops, storage kept) or delete_workstream (hard; removes all traces):
   close_workstream(ws_id='a1b2c3d4', reason='task complete')
   delete_workstream(ws_id='a1b2c3d4')

Wind all direct children down at once → close_all_children (soft-close cascade):
   close_all_children(reason='batch complete, synthesising results')

Plan and track work → tasks (your scratchpad; children don't see it):
   tasks(action='add', title='audit auth.py for CSRF')
   tasks(action='update', task_id='t_03', status='in_progress')
   tasks(action='remove', task_id='t_03')
