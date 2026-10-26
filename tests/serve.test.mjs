import test from 'node:test';
import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import net from 'node:net';
import {fileURLToPath} from 'node:url';

async function unusedPort() {
  const reservation=net.createServer();
  await new Promise((resolve,reject)=>reservation.once('error',reject).listen(0,'127.0.0.1',resolve));
  const port=reservation.address().port;
  await new Promise(resolve=>reservation.close(resolve));
  return port;
}

test('local preview serves browser modules and rejects private project paths', async () => {
  const port=await unusedPort();
  const child=spawn(process.execPath,[fileURLToPath(new URL('../scripts/serve.mjs',import.meta.url))],{
    env:{...process.env,LBR_PORT:String(port),LBR_OPEN_BROWSER:'0'},
    stdio:['ignore','pipe','pipe'],windowsHide:true,
  });
  let output='';
  try{
    await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error(`Preview startup timed out: ${output}`)),5000);
      child.once('error',error=>{clearTimeout(timer);reject(error);});
      child.once('exit',code=>{clearTimeout(timer);reject(Error(`Preview exited ${code}: ${output}`));});
      child.stderr.on('data',chunk=>{output+=chunk;});
      child.stdout.on('data',chunk=>{
        output+=chunk;
        if(output.includes(`LBR preview: http://127.0.0.1:${port}`)){clearTimeout(timer);resolve();}
      });
    });
    const base=`http://127.0.0.1:${port}`;
    for(const route of ['/','/app.js','/portfolio.js','/demo.js','/favicon.svg']){
      const response=await fetch(base+route);
      assert.equal(response.status,200,`${route} should be public`);
      assert.equal(response.headers.get('cache-control'),'no-store');
      if(route.endsWith('.js'))assert.match(response.headers.get('content-type'),/^text\/javascript/);
      await response.arrayBuffer();
    }
    for(const route of ['/DEPLOY.ps1','/.git/config','/scripts/learning.py','/config/strategy.json','/data/learning.json','/data/LBR-ledger-2026.json','/learning/reports/latest.json']){
      const response=await fetch(base+route);
      assert.equal(response.status,404,`${route} must stay private`);
      await response.arrayBuffer();
    }
    assert.equal((await fetch(base+'/',{method:'POST'})).status,405);
    const head=await fetch(base+'/app.js',{method:'HEAD'});
    assert.equal(head.status,200);
    assert.equal(await head.text(),'');
  }finally{
    const stopped=new Promise(resolve=>child.once('exit',resolve));
    if(child.exitCode===null){child.kill();await stopped;}
  }
});

test('an occupied preview port fails before announcing readiness or opening a browser', async () => {
  const reservation=net.createServer();
  await new Promise((resolve,reject)=>reservation.once('error',reject).listen(0,'127.0.0.1',resolve));
  const port=reservation.address().port;
  const child=spawn(process.execPath,[fileURLToPath(new URL('../scripts/serve.mjs',import.meta.url))],{
    env:{...process.env,LBR_PORT:String(port),LBR_OPEN_BROWSER:'1'},
    stdio:['ignore','pipe','pipe'],windowsHide:true,
  });
  let output='';
  child.stdout.on('data',chunk=>{output+=chunk;});
  child.stderr.on('data',chunk=>{output+=chunk;});
  try{
    const code=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>reject(Error('Occupied-port process did not stop')),5000);
      child.once('error',error=>{clearTimeout(timer);reject(error);});
      child.once('exit',code=>{clearTimeout(timer);resolve(code);});
    });
    assert.equal(code,1);
    assert.match(output,/EADDRINUSE/);
    assert.doesNotMatch(output,/LBR preview: http/);
  }finally{
    if(child.exitCode===null)child.kill();
    await new Promise(resolve=>reservation.close(resolve));
  }
});
