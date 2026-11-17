'use strict';
// Offline VM only. Neither the setup nor the expression receives host APIs.
const fs=require('node:fs'),vm=require('node:vm');
globalThis.fetch=()=>{throw Error('OFFLINE_NETWORK_FORBIDDEN');};
require('node:net').Socket.prototype.connect=function(){throw Error('OFFLINE_NETWORK_FORBIDDEN');};
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const context=vm.createContext({window:{}});
vm.runInContext(input.setup,context,{timeout:1000});
Promise.race([vm.runInContext(input.expression,context,{timeout:1000}),
 new Promise((_,reject)=>{const t=setTimeout(()=>reject(Error('OFFLINE_VM_TIMEOUT')),2000);t.unref();})])
 .then(value=>process.stdout.write(JSON.stringify({resolved:true,value:JSON.parse(value),state:vm.runInContext('window.testState || {}',context)})))
 .catch(()=>process.stdout.write(JSON.stringify({resolved:false,error:'expression_rejected'})));
