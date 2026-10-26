import http from 'node:http';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {readFile} from 'node:fs/promises';
import {spawn} from 'node:child_process';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const port=Number(process.env.LBR_PORT||8000);
if(!Number.isInteger(port)||port<1024||port>65535)throw Error('LBR_PORT must be between 1024 and 65535');
const types={'.html':'text/html; charset=utf-8','.js':'text/javascript; charset=utf-8','.mjs':'text/javascript; charset=utf-8','.css':'text/css; charset=utf-8','.svg':'image/svg+xml','.json':'application/json; charset=utf-8','.png':'image/png','.webp':'image/webp'};
const server=http.createServer(async(req,res)=>{
  if(!['GET','HEAD'].includes(req.method)){res.writeHead(405,{Allow:'GET, HEAD'});return res.end();}
  try{
    const pathname=decodeURIComponent(new URL(req.url,'http://127.0.0.1').pathname);
    const relative=pathname==='/'?'index.html':pathname.replace(/^\/+/, '');
    const target=path.resolve(root,relative);
    const isRootAsset=!relative.includes('/')&&/\.(html|css|js|mjs|svg)$/.test(relative);
    const isData=/^data\/[\w.-]+\.json$/.test(relative)&&!/(learning|trades|ledger)/i.test(relative);
    if(!target.startsWith(root+path.sep)||(!isRootAsset&&!isData)||relative.includes('\\')){res.writeHead(404);return res.end('Not found');}
    const data=await readFile(target);
    res.writeHead(200,{'Content-Type':types[path.extname(target)]||'application/octet-stream','Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','X-LBR-Preview':'1'});res.end(req.method==='HEAD'?undefined:data);
  }catch{res.writeHead(404);res.end('Not found');}
});
server.on('error',e=>{console.error(`LBR preview could not start: ${e.message}`);process.exitCode=1;});
server.listen(port,'127.0.0.1',()=>{
  const url=`http://127.0.0.1:${port}`;
  console.log(`LBR preview: ${url}\nPress Ctrl+C to stop.`);
  // Open only after this process successfully owns the port. A port conflict
  // must never open an unrelated service in the user's browser.
  if(process.env.LBR_OPEN_BROWSER==='1'){
    const [command,args]=process.platform==='win32'
      ?['powershell.exe',['-NoProfile','-NonInteractive','-Command',`Start-Process '${url}'`]]
      :process.platform==='darwin'?['open',[url]]:['xdg-open',[url]];
    const opener=spawn(command,args,{stdio:'ignore',windowsHide:true,detached:true});
    opener.on('error',()=>console.warn(`The browser could not be opened automatically. Visit ${url}`));
    opener.unref();
  }
});
for(const signal of ['SIGINT','SIGTERM'])process.on(signal,()=>server.close(()=>process.exit(0)));
