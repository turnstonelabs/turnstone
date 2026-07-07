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

Run a command, git, or tests → bash:
   bash(command='git log -5')
   bash(command='pytest')

Retrieve a URL → web_fetch:
   web_fetch(url='https://example.com')

Show the user content visually (rendered page, PDF, image, data table) → open_preview:
   open_preview(target='https://example.com/pricing')
   open_preview(target='chart.png')
   open_preview(target='results.csv')
User asks to READ/SEE something → open_preview; you need to reason about it yourself → web_fetch / read_file.
Render then show → bash then open_preview:
   bash(command='python plot.py') → open_preview(target='plot.png')

Search the web for information → web_search:
   web_search(query='current population of Tokyo')

