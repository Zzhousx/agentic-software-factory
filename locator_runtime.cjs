function locate(root,s){
  if(s.scope)root=locate(root,s.scope);
  let el;
  if(s.by==='text')el=root.getByText(s.name,{exact:true});
  else if(s.by==='label')el=root.getByLabel(s.name,{exact:true});
  else if(s.by==='placeholder')el=root.getByPlaceholder(s.name,{exact:true});
  else el=root.getByRole(s.role,s.name===undefined?{}:{name:s.name,exact:true});
  if(s.has_text)el=el.filter({hasText:s.has_text});
  return el;
}
async function bindingOptions(page,s){
  if(!s||s.scope||s.has_text||!s.name)return [];
  const options=[];
  for(const by of ['label','placeholder','text']){
    const el=locate(page,{by,name:s.name});const count=await el.count();
    if(count!==1||!await el.isVisible())continue;
    const info=await el.evaluate(e=>{
      const parts=[];let n=e;
      while(n&&n.parentElement){parts.push(Array.prototype.indexOf.call(n.parentElement.children,n));n=n.parentElement;}
      return {element_id:parts.reverse().join('/'),editable:e.matches('input:not([type=checkbox]):not([type=radio]),textarea,[contenteditable=true]'),role:e.getAttribute('role')||(e.tagName==='SELECT'?'combobox':e.type==='checkbox'?'checkbox':e.type==='radio'?'radio':null)};
    });
    options.push({by,name:s.name,count,...info});
  }
  return options;
}
// Chromium discovery: the pinned Playwright's own browser revision is often not
// installed on the runner; fall back to any Chromium/Chrome binary that exists.
function chromiumCandidates(){
  const fs=require('node:fs');const path=require('node:path');const os=require('node:os');
  const out=[];const add=(p)=>{if(p&&!out.includes(p)&&fs.existsSync(p))out.push(p);};
  add(process.env.EVOGIT_CHROMIUM_PATH);add(process.env.CHROME_PATH);add(process.env.PUPPETEER_EXECUTABLE_PATH);
  const roots=[process.env.PLAYWRIGHT_BROWSERS_PATH,'/ms-playwright',path.join(os.homedir(),'.cache','ms-playwright'),
    path.join(os.homedir(),'AppData','Local','ms-playwright'),path.join(os.homedir(),'Library','Caches','ms-playwright')].filter(Boolean);
  for(const root of roots){
    let entries=[];try{entries=fs.readdirSync(root).sort().reverse();}catch{continue;}
    for(const name of entries){
      if(!/^chromium/.test(name))continue;
      for(const rel of ['chrome-linux64/chrome','chrome-linux/chrome','chrome-headless-shell-linux64/chrome-headless-shell',
        'chrome-linux/headless_shell','chrome-win/chrome.exe','chrome-win64/chrome.exe','chrome-mac/Chromium.app/Contents/MacOS/Chromium'])add(path.join(root,name,rel));
    }
  }
  for(const p of ['/usr/bin/chromium','/usr/bin/chromium-browser','/usr/bin/google-chrome','/usr/bin/google-chrome-stable',
    '/opt/google/chrome/chrome','/snap/bin/chromium','C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'])add(p);
  return out;
}
async function launchChromium(chromium){
  const args=['--no-sandbox','--disable-dev-shm-usage'];
  const errors=[];
  try{return await chromium.launch({headless:true,args});}catch(error){errors.push(String(error.message||error).split('\n')[0]);}
  for(const executablePath of chromiumCandidates()){
    try{return await chromium.launch({headless:true,args,executablePath});}catch(error){errors.push(executablePath+': '+String(error.message||error).split('\n')[0]);}
  }
  throw new Error('No launchable Chromium. '+errors.join(' | ').slice(0,600));
}
module.exports={locate,bindingOptions,launchChromium,chromiumCandidates};
