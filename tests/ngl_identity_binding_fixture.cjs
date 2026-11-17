'use strict';
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
globalThis.fetch=()=>{throw Error('OFFLINE_NETWORK_FORBIDDEN');};
require('node:net').Socket.prototype.connect=function(){throw Error('OFFLINE_NETWORK_FORBIDDEN');};
const input=JSON.parse(fs.readFileSync(0,'utf8')),root=process.env.NGL_TEST_GATEWAY_ROOT;
if(!root)throw Error('Explicit read-only NGL source root is required');
const identity=require(path.join(root,'ngl/server/src/lib/evaluation-identity'));
const gateway=require(path.join(root,'ngl/server/src/lib/ai-gateway'));
const old={...input.before,sidecarSourceSha256:'d'.repeat(64)},next={...input.after,sidecarSourceSha256:'d'.repeat(64)};
(async()=>{
 const oldFP=identity.digest(identity.snapshot(old)),newFP=identity.digest(identity.snapshot(next));assert.notEqual(oldFP,newFP);
 const preset={id:'synthetic',name:'Synthetic',provider:'openai',endpoint:'tunnel://workbuddy-proxy',model:'deepseek-v4-flash',timeout:5000};
 const settings={ai_presets:{value:[preset]},active_ai_preset:{value:{presetId:preset.id}}};let chats=0;
 const store={getUserSetting:async(_user,key)=>settings[key],setUserSetting:async()=>{throw Error('SETTINGS_WRITE_FORBIDDEN');}};
 const tunnel={getStatus:()=>({connected:true,authenticatedGeneration:1}),request:async(_body,options)=>{
  if(options.operation==='models')return{status:200,body:{data:Object.keys(next.mappings).map(id=>({id}))}};
  if(options.operation==='evaluation_snapshot')return{status:200,body:next};
  chats++;throw Error('MODEL_DISPATCH_FORBIDDEN');
 }};
 const routes=new Map(),app={get:()=>{},put:()=>{},post:(route,handler)=>routes.set(route,handler)};
 gateway.registerRoutes(app,{store,tunnel,wrap:handler=>handler,errRes:()=>{throw Error('UNEXPECTED_ERROR_RESPONSE');}});
 const req={body:{model:preset.model,messages:[{role:'user',content:'synthetic'}],max_tokens:64,temperature:0,
  identityProtocol:identity.PROTOCOL,identityFingerprint:oldFP},on:()=>{},removeListener:()=>{}};
 const res={on:()=>{},removeListener:()=>{},writableEnded:false,destroyed:false};
 await assert.rejects(routes.get('/api/ai/evaluation/chat/completions')(req,res),{code:'AI_EVALUATION_CONTEXT_CHANGED'});
 assert.equal(chats,0);
 const missing={...next};delete missing.proxySourceSha256;
 assert.throws(()=>identity.snapshot(missing),{code:'AI_EVALUATION_CAPABILITY_REQUIRED'});
 process.stdout.write(JSON.stringify({identityFingerprintChanged:true,oldFingerprintRejected:true,evaluationChats:chats,legacyCannotReplaceBoundField:true}));
})().catch(error=>{process.stderr.write(error.code||error.message);process.exitCode=1;});
