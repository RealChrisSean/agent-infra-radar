// Browser checks use synthetic fixtures and a local server. No email is sent.
import assert from 'node:assert/strict';
import http from 'node:http';
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const require=createRequire(import.meta.url);
const {chromium}=require('playwright');
const root=path.dirname(fileURLToPath(import.meta.url));
const settings={focus:'Agent infrastructure software engineering',min_salary:150000,salary_rule:'minimum',remote_allowed:true,remote_region:'United States',locations:['San Francisco'],include_keywords:['agent','infrastructure'],exclude_keywords:['sales'],profile_text:'',email_enabled:true,email_to:'',email_from:'',connector_base:'',schedule_enabled:true,schedule_hour:16,schedule_minute:0,timezone:'America/Los_Angeles',sources:[{provider:'ashby',slug:'example',company:'Example',enabled:true}]};
const fixture={settings,jobs:[],runs:[],notifications:{configured:false,connector_ready:false,message:'Connect Resend to this Sprite to enable email delivery.',pending:0,sent:0,emails_sent:0},scan:{running:false},schedule:{status:'not_configured',message:'Daily schedule has not been connected yet.'}};
let previewFixture={subject:'Your job match preview',text:'A matching role at Example. No email has been sent.',job_count:1,shown_count:1,more_count:0};
const mutations=[],errors=[];
const server=http.createServer(async(req,res)=>{
  try{
    const chunks=[];for await(const chunk of req)chunks.push(chunk);
    const body=chunks.length?JSON.parse(Buffer.concat(chunks).toString()):{};
    if(req.method!=='GET'){assert.equal(req.headers['x-radar-client'],'1');mutations.push({path:req.url,body});}
    res.setHeader('Content-Security-Policy',"default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'");
    if(req.url.startsWith('/api/')){
      res.setHeader('Content-Type','application/json');
      let result=fixture;
      if(req.url==='/api/infra/settings'){Object.assign(settings,body);result={settings};}
      else if(req.url.startsWith('/api/infra/jobs/')){const job=fixture.jobs.find(job=>job.id===req.url.split('/').pop());Object.assign(job,body);result={job};}
      else if(req.url==='/api/infra/preview')result=previewFixture;
      else if(req.url==='/api/infra/notify')result={...fixture.notifications,sent_now:144,highlighted_now:10,remaining_now:134,emails_sent_now:1};
      else if(req.url==='/api/infra/scan'){fixture.scan.running=true;result={status:'started'};}
      else if(req.url==='/api/infra/connector')result=fixture.notifications;
      res.end(JSON.stringify(result));return;
    }
    const filename=req.url==='/'?'infra.html':req.url.slice(1);
    if(!['infra.html','infra.css','infra.js'].includes(filename)){res.statusCode=404;res.end();return;}
    res.setHeader('Content-Type',filename.endsWith('.html')?'text/html':filename.endsWith('.css')?'text/css':'text/javascript');
    res.end(await readFile(path.join(root,'public',filename)));
  }catch(error){res.statusCode=500;res.end(error.message);errors.push(error);}
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const browser=await chromium.launch({headless:true,channel:process.env.RADAR_BROWSER_CHANNEL||'chrome'});
try{
  const page=await browser.newPage({viewport:{width:1440,height:1080}});
  page.on('pageerror',error=>errors.push(error));
  const url=`http://127.0.0.1:${server.address().port}`;
  await page.goto(url);await page.waitForFunction(()=>document.querySelector('#matches-count').textContent==='0');
  assert.equal(await page.locator('#empty-title').innerText(),'Your next opportunity is out there.');
  assert.equal(await page.locator('#send-email').isDisabled(),true);
  assert.equal(mutations.length,0,'Opening the page must not send or mutate anything.');
  await page.locator('#settings-open').click();
  await page.locator('[name=min_salary]').fill('175000');
  await page.locator('[name=locations]').fill('San Francisco, Oakland');
  await page.locator('[name=include_keywords]').fill('agents, runtime\norchestration');
  await page.locator('#settings-save').click();
  await page.waitForFunction(()=>!document.querySelector('#settings-dialog').open);
  assert.equal(settings.min_salary,175000);assert.deepEqual(settings.locations,['San Francisco','Oakland']);assert.deepEqual(settings.include_keywords,['agents','runtime','orchestration']);
  assert.equal(settings.schedule_hour,16,'Hidden schedule metadata must be preserved.');
  fixture.jobs=[{id:'one',company:'Example',title:'Software Engineer, Agent Runtime',match_status:'match',salary_min:180000,salary_max:230000,salary_currency:'USD',salary_interval:'year',location:'Remote, United States',reasons:['Agent runtime engineering','Published base minimum meets your target'],caveats:['Review the experience requirements'],first_seen:'2026-10-08T12:00:00Z',verified_at:'2026-10-08T12:00:00Z',source:'ashby',source_url:'https://example.invalid/jobs',url:'https://example.invalid/jobs/one'}, {id:'two',company:'Another',title:'Platform Engineer',match_status:'review',salary_min:null,salary_max:null,location:'San Francisco',reasons:[],caveats:['Salary is not published'],first_seen:'2026-10-07T12:00:00Z'}, {id:'three',company:'No Match',title:'Sales Engineer',match_status:'excluded',location:'New York',first_seen:'2026-10-07T12:00:00Z'}];
  fixture.runs=[{status:'partial',completed_at:'2026-10-08T12:01:00Z',summary:'One feed failed; other results were saved.',coverage:[{company:'Example',status:'completed',count:12},{company:'Another',status:'failed',error:'Feed unavailable'}]}];
  await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.job-card').length===1);
  assert.match(await page.locator('#run-title').innerText(),/partially completed/);
  assert.match(await page.locator('.job-card').innerText(),/\$180,000 – \$230,000 \/ year/);
  await page.locator('#history-toggle').click();assert.match(await page.locator('#history').innerText(),/Another: failed · Feed unavailable/);
  await page.locator('.save-action').click();await page.locator('[data-view=saved]').click();assert.equal(await page.locator('.job-card').count(),1);
  await page.locator('[data-view=review]').click();assert.match(await page.locator('.job-card').innerText(),/Salary is not published/);
  await page.locator('[data-view=match]').click();await page.locator('#query').fill('does not exist');assert.equal(await page.locator('#empty').isVisible(),true);await page.locator('#empty-action').click();assert.equal(await page.locator('.job-card').count(),1);
  await page.locator('#preview-email').click();await page.waitForFunction(()=>document.querySelector('#preview-dialog').open);assert.match(await page.locator('#preview-body').innerText(),/No email has been sent/);assert.match(await page.locator('#preview-note').innerText(),/1 matching role in one email/);await page.locator('#preview-done').click();assert.equal(mutations.filter(item=>item.path==='/api/infra/notify').length,0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Desktop must not overflow.');
  if(process.env.RADAR_UI_SCREENSHOT)await page.screenshot({path:process.env.RADAR_UI_SCREENSHOT,fullPage:true});
  await page.setViewportSize({width:390,height:844});
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Mobile must not overflow.');
  await page.locator('#settings-open').click();assert.equal(await page.locator('#settings-dialog').isVisible(),true);
  assert.equal(await page.evaluate(()=>document.querySelector('#settings-dialog').scrollWidth<=document.querySelector('#settings-dialog').clientWidth),true,'Mobile settings must not overflow.');
  if(process.env.RADAR_UI_MOBILE_SCREENSHOT)await page.screenshot({path:process.env.RADAR_UI_MOBILE_SCREENSHOT,fullPage:true});
  await page.locator('#settings-close').click();
  fixture.notifications={configured:true,connector_ready:true,pending:144,sent:144,emails_sent:1};
  await page.reload();await page.waitForFunction(()=>!document.querySelector('#send-email').disabled);
  assert.match(await page.locator('#email-description').innerText(),/top 10 of 144 new matches will be emailed/);
  assert.match(await page.locator('#email-description').innerText(),/remaining 134 in the app/);
  assert.equal(await page.locator('#send-email').innerText(),'Email top 10 matches');
  assert.equal(await page.locator('#sent-count').innerText(),'1','Metric counts delivered digests, not all jobs covered by a digest.');
  previewFixture={subject:'Your top 10 job matches',text:'Ten selected roles. View the remaining 77 in the app: https://example.invalid/',job_count:87,shown_count:10,more_count:77};
  await page.locator('#preview-email').click();await page.waitForFunction(()=>document.querySelector('#preview-dialog').open);
  assert.match(await page.locator('#preview-note').innerText(),/10 best matches of 87 in one email/);
  assert.match(await page.locator('#preview-note').innerText(),/view the remaining 77/);
  await page.locator('#preview-done').click();
  assert.equal(mutations.filter(item=>item.path==='/api/infra/notify').length,0,'A top-10 preview must not send email.');
  await page.locator('#send-email').click();await page.waitForFunction(()=>document.querySelector('#toast').textContent.includes('Top 10 matches sent in 1 email'));
  assert.match(await page.locator('#toast').innerText(),/View the remaining 134 in the app/);
  assert.doesNotMatch(await page.locator('#toast').innerText(),/144 matches sent/);
  assert.equal(mutations.filter(item=>item.path==='/api/infra/notify').length,1,'Digest is dispatched once, not once per job.');
  if(process.env.RADAR_UI_SNAPSHOT){
    Object.assign(fixture,JSON.parse(await readFile(process.env.RADAR_UI_SNAPSHOT,'utf8')));
    await page.setViewportSize({width:1440,height:1100});await page.reload();await page.waitForFunction(()=>document.querySelectorAll('.job-card').length>1);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Actual job data must fit desktop.');
    if(process.env.RADAR_UI_ACTUAL_DESKTOP)await page.screenshot({path:process.env.RADAR_UI_ACTUAL_DESKTOP});
    await page.setViewportSize({width:390,height:844});
    for(const view of ['match','review','excluded']){await page.locator(`[data-view=${view}]`).click();assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Actual ${view} data must fit mobile.`);}
    await page.locator('[data-view=match]').click();
    if(process.env.RADAR_UI_ACTUAL_MOBILE)await page.screenshot({path:process.env.RADAR_UI_ACTUAL_MOBILE});
  }
  assert.deepEqual(errors,[]);
  console.log('PASS: initial state, safe settings changes, match/review filters, saved roles, failure coverage, read-only top-10 preview, digest counts and overflow copy, desktop/mobile layout'+(process.env.RADAR_UI_SNAPSHOT?', actual job data.':'.'));
}finally{await browser.close();await new Promise(resolve=>server.close(resolve));}
