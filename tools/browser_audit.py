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
        page=browser.new_page(viewport={'width':1280,'height':1000},reduced_motion='reduce')
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(os.environ.get('AUDIT_URL',f'http://127.0.0.1:{server.server_port}'),wait_until='networkidle')
        page.wait_for_function('window.__halo?.ready')
        assert page.evaluate('__halo.passages')>=8
        assert page.evaluate('__halo.matches')>0
        first=page.locator('#sentence').inner_text()
        page.locator('#next').click()
        assert page.locator('#sentence').inner_text()!=first
        page.locator('#back').click()
        assert page.locator('#sentence').inner_text()==first
        for pack in ['incident','research','handoff','pilot']:
            page.locator('#pack').select_option(pack)
            assert page.evaluate('__halo.matches')>0
        page.locator('#note-editor summary').click()
        page.locator('#note-title').fill('Custom decision')
        page.locator('#note-text').fill('# Orchard review\n\nThe orchard meeting needs seven crates of apples. Alice brings the crates.')
        page.locator('#add-note').click()
        page.locator('#question').fill('orchard crates');page.locator('#question-form button').click()
        assert 'seven crates' in page.locator('#sentence').inner_text()
        page.locator('[data-source]').first.click()
        assert page.locator('#source').is_visible()
        assert 'seven crates' in page.locator('#source-text').inner_text()
        page.locator('#close-source').click()
        page.locator('#question').fill('zzzz submarine');page.locator('#question-form button').click()
        assert page.evaluate('__halo.matches')==0
        assert 'No matching' in page.locator('#status').inner_text()
        with page.expect_download() as dl:page.locator('#export').click()
        assert 'seven crates' in Path(dl.value.path()).read_text()
        page.locator('#reset').click()
        page.locator('#note-editor summary').click()
        page.evaluate('window.scrollTo(0,0)')
        page.screenshot(path=str(ROOT/'examples/portfolio/preview.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),'mobile overflow'
        assert page.locator('#question-form').bounding_box()['y'] < page.locator('#documents').bounding_box()['y']
        assert not errors,errors
        print('PASS: four packs, custom notes, source inspection, no-evidence handling, sentence navigation and export')
        browser.close()
finally: server.shutdown()
