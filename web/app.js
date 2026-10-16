'use strict';
const tg = window.Telegram?.WebApp;
const auth = tg?.initData || '';
const $ = s => document.querySelector(s);
let state = null, tab = 'home', room = null, busy = false, toastTimer, detailID = null;
const names = {light:'Светлая',dark:'Тёмная',cozy:'Уютная дача'};
function el(tag, cls, text) { const node=document.createElement(tag); if(cls)node.className=cls; if(text!==undefined)node.textContent=text; return node; }
function btn(cls, text, fn) {const b=el('button',cls,text);b.type='button';b.onclick=fn;return b;}
function toast(message){const node=$('#toast');node.textContent=message;node.classList.add('visible');clearTimeout(toastTimer);toastTimer=setTimeout(()=>node.classList.remove('visible'),6500);}
function haptic(){try{tg?.HapticFeedback?.impactOccurred('light');}catch{}}
function theme(value){document.documentElement.dataset.theme=value;const color={light:'#f7f9fc',dark:'#11151b',cozy:'#f5f0e7'}[value]||'#11151b';try{tg?.setHeaderColor(color);tg?.setBackgroundColor(color);}catch{}}
async function api(path, body){
 const response=await fetch('/api/'+path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Telegram-Init-Data':auth},...(body===undefined?{}:{body:JSON.stringify(body)}),cache:'no-store',signal:AbortSignal.timeout(60000)});
 const data=await response.json(); if(!response.ok)throw new Error(data.error || 'Не удалось выполнить запрос.');return data;
}
function accept(data){state=data;theme(data.theme);render();$('#sync').textContent='Обновлено в '+new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'})+' · Данные Яндекса могут обновляться с задержкой';}
async function mutate(path,body){if(busy)return;busy=true;lock(true);try{accept(await api(path,body));haptic();}catch(e){toast(e.name==='TimeoutError'?'Нет ответа сервера. Проверьте состояние перед повтором команды.':e.message);}finally{busy=false;lock(false);}}
function lock(value){document.querySelectorAll('button,input').forEach(n=>n.disabled=value||n.dataset.locked==='1');$('#app').setAttribute('aria-busy',String(value));}
async function reload(force=false){if(busy)return;busy=true;lock(true);try{accept(await api('state'+(force?'?refresh=1':'')));}catch(e){if(!state)failure(e.message);else toast(e.message);}finally{busy=false;lock(false);}}
function heading(title,description){const box=el('div','heading'),text=el('div');text.append(el('h2',null,title));if(description)text.append(el('p',null,description));box.append(text);return box;}
function empty(title,description){const n=el('div','empty');n.append(el('div','empty-icon','✧'),el('h3',null,title),el('p',null,description));return n;}
function go(next){tab=next;room=null;render();window.scrollTo({top:0,behavior:'smooth'});}
function artFor(d){const n=d.name.toLowerCase();return /гирлянд/.test(n)?'string':/прожектор/.test(n)?'flood':/балкон|бра|стен/.test(n)?'wall':'lamp';}
function artwork(d){const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('class','art');svg.setAttribute('aria-hidden','true');const use=document.createElementNS(svg.namespaceURI,'use');use.setAttribute('href','/art.svg#'+artFor(d));svg.append(use);return svg;}
function card(d){
 const n=el('article','device'+(d.on===true?' on':''));
 const star=btn('star'+(d.favorite?' chosen':''),d.favorite?'★':'☆',()=>mutate('favorite',{home:state.home,device:d.id,enabled:!d.favorite}));
 star.setAttribute('aria-label',(d.favorite?'Убрать из избранного: ':'В избранное: ')+d.name);star.setAttribute('aria-pressed',String(d.favorite));
 const title=btn('device-title',d.name,()=>openDetail(d.id));
 const status=d.reachable==='offline'?'Нет связи':d.on===true?'Включён':d.on===false?'Выключен':'Состояние неизвестно';
 const power=btn('power','',()=>command(d,'on',d.on===true?0:1));
 power.append(el('span','power-label',d.on===true?'Выключить':'Включить'),el('span','toggle'));
 power.setAttribute('aria-label',(d.on===true?'Выключить ':'Включить ')+d.name);
 power.setAttribute('aria-pressed',String(d.on===true));
 if(!d.can_switch||d.reachable==='offline'){power.disabled=true;power.dataset.locked='1';}
 n.append(star,artwork(d),title,el('p','status',status),power);return n;
}
function render(){
 if(!state)return;
 $('#app').setAttribute('aria-busy','false');$('#nav').hidden=false;
 document.querySelectorAll('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===tab));
 const homes=$('#homes');homes.replaceChildren();homes.hidden=!state.homes.length;
 for(const h of state.homes){const b=btn('home-pill'+(h.id===state.home?' active':''),'',()=>{room=null;closeDetail();mutate('home',{home:h.id});});b.append(el('span',null,'⌂ '+h.name),el('span','count',String(h.count)));b.setAttribute('aria-pressed',String(h.id===state.home));homes.append(b);}
 const main=$('#content');main.replaceChildren();
 if(!state.home&&tab!=='settings'){main.append(empty('Выберите дом',state.homes.length?'Нажмите на название дома сверху.':'Добавьте дом в приложении «Дом с Алисой», затем обновите панель.'));return;}
 if(tab==='settings'){renderSettings(main);return;}
 if(tab==='rooms'&&!room){main.append(heading('Комнаты','Свет там, где он нужен'));const list=el('div','room-list');for(const r of state.rooms){const count=state.devices.filter(d=>d.room===r.id).length;if(!count)continue;const b=btn('room-button','',()=>{room=r.id;render();});b.append(el('span','room-icon','▦'),el('span',null,r.name),el('small',null,count+' устр.  ›'));list.append(b);}if(!list.children.length)list.append(empty('Здесь пока пусто','Добавьте розетки и выключатели света в настройках.'));main.append(list);return;}
 if(tab==='rooms'&&room)main.append(btn('quiet back','‹ Все комнаты',()=>{room=null;render();}));
 const devices=state.devices.filter(d=>tab==='home'?d.favorite:tab==='rooms'?d.room===room:true);
 const title=tab==='home'?'Избранное':tab==='rooms'?(state.rooms.find(r=>r.id===room)?.name||'Комната'):'Все устройства';
 main.append(heading(title,tab==='home'?'Всё нужное под рукой':devices.length+' устройств в выбранном доме'));
 if(devices.length){const grid=el('div','grid');devices.forEach(d=>grid.append(card(d)));main.append(grid);}else main.append(empty(tab==='home'?'Добавьте любимый свет':'Свет пока не добавлен',tab==='home'?'Откройте все устройства и нажмите ☆ на нужных карточках.':'В настройках можно добавить розетки и выключатели, к которым подключено освещение.'));
 if(tab!=='all'){const all=btn('all-button','',()=>go('all'));all.append(el('span',null,'☷  Все устройства'),el('span',null,'›'));main.append(all);}
 if(!state.devices.length)main.append(btn('all-button','➕ Добавить устройство в свет',()=>go('settings')));
 if(busy)lock(true);
}
function renderSettings(main){
 main.append(heading('Настройки','Ваш дом. Ваше настроение.'));
 const panel=el('section','settings-panel');panel.append(el('h3',null,'Оформление'),el('p',null,'Выберите тему — она сохранится для вашего аккаунта.'));
 const themes=el('div','themes');for(const [value,name] of Object.entries(names)){const b=btn('theme '+value+(state.theme===value?' selected':''),'',()=>mutate('settings',{theme:value}));b.setAttribute('aria-pressed',String(state.theme===value));const preview=el('span','theme-preview');preview.append(el('i'),el('i'));b.append(preview,el('span',null,name));themes.append(b);}panel.append(themes);main.append(panel);
 const notify=el('section','settings-panel');notify.append(el('h3',null,'Уведомления'),el('p',null,'Потеря связи и восстановление устройств во всех домах. Уведомления приходят в чат бота.'));
 const nr=el('div','setting-row');nr.append(el('span',null,'Получать уведомления'),btn('',state.alerts?'Включены':'Выключены',()=>mutate('settings',{alerts:!state.alerts})));notify.append(nr);main.append(notify);
 const extras=el('section','settings-panel');extras.append(el('h3',null,'Устройства света'),el('p',null,'Отметьте розетки и выключатели, управляющие освещением. Список общий для всех пользователей. Добавление не включает питание.'));
 for(const d of state.candidates){const row=el('div','candidate');row.append(el('p',null,d.name));if(d.locked)row.append(el('span','info','Задано на сервере'));else{const b=btn('',d.added?'✓ Добавлено':'+ Добавить',()=>mutate('extra',{home:state.home,device:d.id,enabled:!d.added}));b.setAttribute('aria-label',(d.added?'Убрать из света ':'Добавить в свет ')+d.name);row.append(b);}extras.append(row);}
 if(!state.candidates.length)extras.append(el('p',null,state.home?'Подходящих розеток и выключателей в этом доме нет.':'Сначала выберите дом сверху.'));main.append(extras);
 main.append(el('p','sync','Мой умный дом · версия 2.0'));
}
function closeDetail(){detailID=null;$('#details').close();}
function openDetail(id){const d=state.devices.find(x=>x.id===id);if(!d)return;detailID=id;const content=$('#detail-content');content.replaceChildren(artwork(d),el('h2',null,d.name),el('p',null,state.rooms.find(r=>r.id===d.room)?.name||'Без комнаты'));
 const power=btn('primary',d.on===true?'Выключить':'Включить',()=>command(d,'on',d.on===true?0:1));if(!d.can_switch||d.reachable==='offline'){power.disabled=true;power.dataset.locked='1';}content.append(power);
 function slider(title,kind,range,value){if(!range)return;const box=el('section','control');const label=el('label',null,title),output=el('span',null,' '+value),input=el('input');input.type='range';input.min=range.min;input.max=range.max;input.step=range.precision||1;input.value=value;const unique='range-'+kind;input.id=unique;label.htmlFor=unique;label.append(output);input.oninput=()=>output.textContent=' '+input.value;box.append(label,input,btn('primary','Применить',()=>command(d,kind,Number(input.value))));content.append(box);}
 slider('Яркость, %','brightness',d.brightness_range,d.brightness??50);slider('Температура, K','temperature_k',d.temperature_range,d.temperature_range?.min);
 if(d.rgb){const box=el('section','control');box.append(el('label',null,'Цвет'));const colors=el('div','colors');for(const [name,v,label]of[['red',16711680,'Красный'],['green',65280,'Зелёный'],['blue',255,'Синий'],['yellow',16776960,'Жёлтый']]){const b=btn('swatch '+name,'',()=>command(d,'rgb',v));b.setAttribute('aria-label',label);colors.append(b);}box.append(colors);content.append(box);}
 if(!$('#details').open)$('#details').showModal();}
async function command(d,kind,value){if(busy)return;busy=true;lock(true);const id=crypto.randomUUID();try{const result=await api('action',{request_id:id,home:state.home,device:d.id,kind,value});accept(result.state);haptic();toast(result.message);if(detailID)openDetail(detailID);}catch(e){toast(e.name==='TimeoutError'?'Нет ответа. Команда могла выполниться. Обновите состояние перед повтором.':e.message);}finally{busy=false;lock(false);}}
function failure(message){$('#content').replaceChildren(empty('Откройте панель из бота',message));$('#homes').hidden=true;$('#nav').hidden=true;$('#sync').textContent='Доступ только для пользователей белого списка.';}
$('#refresh').onclick=()=>reload(true);$('#close-dialog').onclick=closeDetail;$('#details').addEventListener('cancel',()=>detailID=null);
document.querySelectorAll('[data-tab]').forEach(b=>b.onclick=()=>go(b.dataset.tab));
if(tg){tg.ready();tg.expand();try{tg.SettingsButton?.show();tg.onEvent('settingsButtonClicked',()=>go('settings'));}catch{}}
if(auth){reload();setInterval(()=>{if(!document.hidden&&!busy&&!$('#details').open)reload();},60000);}else{failure('Нажмите «Открыть панель» в личном чате с ботом. Внешняя ссылка не даёт доступа к устройствам.');}
