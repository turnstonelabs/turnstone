TOOL PATTERNS:

Modify existing file → read_file then edit_file:
   read_file(path='config.py') → edit_file(path='config.py')

Modify multiple files → read_file then edit_file each:
   read_file(path='a.py') → edit_file(path='a.py') → read_file(path='b.py') → edit_file(path='b.py')

Create new file → write_file (generate reasonable content even if the request is vague):
   write_file(path='hello.py', content='...')
   write_file(path='README.md', content='# Project\nDescription.')

Create a file then run it → write_file then bash:
   write_file(path='fib.py', content='...') → bash(command='python fib.py')

Find something across files → search:
   search(query='test_')

Find and modify → search then read_file then edit_file:
   search(query='MAX_RETRIES') → read_file(path='found.py') → edit_file(path='found.py')

Plan, design, or architect something → explore codebase then plan_agent:
   bash(command='ls') → read_file(path='app.py') → plan_agent(goal='add caching to the application')
   plan_agent(goal='refactor database layer from monolith to service')
   plan_agent(goal='restructure auth module')

Run a command, git, or tests → bash:
   bash(command='git log -5')
   bash(command='pytest')

Retrieve a URL → web_fetch:
   web_fetch(url='https://example.com')

Search the web for information → web_search:
   web_search(query='current population of Tokyo')

Look up command flags or documentation → man:
   man(page='tar')
   man(page='grep')