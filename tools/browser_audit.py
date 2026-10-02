"""Exercise meeting notes, evidence retrieval and sentence reading locally or against its public deployment."""
import functools
import http.server
import os
import threading
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self,*args): pass
server=http.server.ThreadingHTTPServer(('127.0.0.1',0),functools.partial(Quiet,directory=str(ROOT/'examples/portfolio')))
threading.Thread(target=server.serve_forever,daemon=True).start()
try:
    with sync_playwright() as p:
        browser=p.chromium.launch(**({'channel':'chrome'} if os.name=='nt' else {}))
        page=browser.new_page(viewport={'width':1440,'height':1100},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(os.environ.get('AUDIT_URL',f'http://127.0.0.1:{server.server_port}'),wait_until='networkidle')
        page.wait_for_function('window.__halo?.ready')
        assert page.evaluate('__halo.passages')>=8
        assert page.evaluate('__halo.matches')>0
        assert page.evaluate('__halo.stage')=='fast answer'
        assert page.locator('[data-prompt]').count() == 3
        assert 'notes' in page.locator('#grounding').inner_text().lower()
        assert 'ms local' in page.locator('#latency').inner_text().lower()
        page.wait_for_function("window.__halo.stage === 'clever answer'")
        assert page.locator('#latency').inner_text().strip().lower() == '/ 1.10s staged'
        page.locator('[data-prompt]').nth(0).click()
        page.locator('[data-prompt]').nth(1).click()
        page.locator('[data-prompt]').nth(2).click()
        assert page.locator('#question').input_value() == 'When is the readiness review?'
        page.wait_for_function("window.__halo.stage === 'clever answer'")
        first=page.locator('#sentence').inner_text()
        assert 'Thursday' in page.locator('#full-answer').text_content()
        page.locator('#next').click()
        assert page.locator('#sentence').inner_text()!=first
        page.locator('#back').click()
        assert page.locator('#sentence').inner_text()==first
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => { window.__copied = text; }}, configurable: true})")
        page.locator('#copy-answer').click()
        page.wait_for_function('window.__copied !== undefined')
        assert page.evaluate("window.__copied === document.querySelector('#full-answer').textContent.trim()")
        assert page.locator('#copy-status').inner_text() == 'answer copied'
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => { throw Error('denied'); }}, configurable: true})")
        page.locator('#copy-answer').click()
        page.wait_for_function("document.querySelector('#copy-status').textContent==='select the full answer to copy it'")
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async text => { window.__copied = text; }}, configurable: true})")
        page.locator('[data-prompt]').nth(0).click()
        assert page.locator('#copy-status').inner_text() == ''
        for pack in ['incident','research','handoff','pilot']:
            page.locator('#pack').select_option(pack)
            assert page.evaluate('__halo.matches')>0
        page.locator('#note-editor summary').click()
        page.locator('#note-title').fill('Custom decision')
        page.locator('#note-text').fill('# Orchard review\n\nThe orchard meeting needs seven crates of apples. Alice brings the crates.')
        page.locator('#add-note').click()
        page.locator('#question').fill('orchard crates');page.locator('#question-form button[type="submit"]').click()
        assert 'seven crates' in page.locator('#sentence').inner_text()
        page.locator('[data-source]').first.click()
        assert page.locator('#source').is_visible()
        assert 'seven crates' in page.locator('#source-text').inner_text()
        page.keyboard.press('Escape')
        assert page.locator('#source').is_hidden()
        page.locator('#question').fill('zzzz submarine');page.locator('#question-form button[type="submit"]').click()
        assert page.evaluate('__halo.matches')==0
        assert 'no grounded' in page.locator('#status').inner_text().lower()
        assert page.locator('#copy-answer').is_disabled()
        assert 'No matching passage' in page.locator('#evidence').inner_text()
        page.locator('#files').set_input_files({'name':'large.md','mimeType':'text/markdown','buffer':b'x'*250001})
        assert '250 KB' in page.locator('#note-status').inner_text()
        page.locator('#add-note').click()
        assert 'Paste some notes' in page.locator('#note-status').inner_text()
        page.locator('#files').set_input_files({'name':'upload.md','mimeType':'text/markdown','buffer':b'# Uploaded\n\nThe amber review needs a written rollback owner.'})
        page.wait_for_function('__halo.documents===4')
        page.locator('#files').set_input_files({'name':'upload.md','mimeType':'text/markdown','buffer':b'# Uploaded\n\nThe amber review is still open.'})
        page.wait_for_function('__halo.documents===5')
        with page.expect_download() as dl:page.locator('#export').click()
        assert 'seven crates' in Path(dl.value.path()).read_text()
        page.locator('#reset').click()
        page.wait_for_function("window.__halo.stage === 'clever answer'")
        page.locator('.reader').focus();page.keyboard.press('ArrowRight')
        assert page.evaluate('__halo.position')==1
        page.keyboard.press('ArrowLeft')
        assert page.evaluate('__halo.position')==0
        page.locator('#copy-answer').click();page.locator('#copy-answer').click()
        assert page.locator('#copy-status').inner_text()=='answer copied'
        page.locator('.cue-editor summary').click()
        page.locator('#cue').fill('An edited cue. A second sentence.')
        page.locator('#apply-cue').click()
        page.wait_for_timeout(1200)
        assert page.evaluate('__halo.stage')=='edited cue'
        assert 'edited text' in page.locator('#grounding').inner_text()
        page.locator('.cue-editor summary').click()
        page.locator('#reset').click()
        page.wait_for_function("window.__halo.stage === 'clever answer'")
        page.locator('#note-editor summary').click()
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(ROOT/'examples/portfolio/preview.png'),full_page=True)
        page.set_viewport_size({'width':390,'height':844})
        (ROOT/'output'/'playwright').mkdir(parents=True,exist_ok=True)
        page.screenshot(path=str(ROOT/'output'/'playwright'/'mobile.png'),full_page=True)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile overflow'
        assert page.locator('#sentence').is_visible()
        assert page.evaluate("getComputedStyle(document.querySelector('#copy-answer')).transitionDuration")=='0s'
        failed=browser.new_page()
        failed.route('**/data/meetings.json',lambda route:route.fulfill(status=503,body='unavailable'))
        failed.goto(f'http://127.0.0.1:{server.server_port}',wait_until='networkidle')
        assert 'Refresh to retry' in failed.locator('#status').inner_text()
        assert failed.locator('#question-form button[type=submit]').is_disabled()
        assert not errors,errors
        print('PASS: four packs, rapid prompts, custom notes, source/Escape, no-evidence state, keyboard navigation, repeated copy, edited cue, upload rejection, export, desktop/mobile390, reduced motion and load error; no page errors')
        browser.close()
finally: server.shutdown()
