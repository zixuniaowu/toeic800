(function(){
  var v=document.getElementById('player'); if(!v) return;
  var btns=[].slice.call(document.querySelectorAll('.segs button'));
  btns.forEach(function(b){b.addEventListener('click',function(){
    var t=parseFloat(b.dataset.t)||0;
    var go=function(){try{v.currentTime=t;}catch(e){} v.play&&v.play().catch(function(){});};
    if(v.readyState>=1){go();}else{v.addEventListener('loadedmetadata',go,{once:true}); v.load();}
    v.scrollIntoView({behavior:'smooth',block:'center'});
  });});
  v.addEventListener('timeupdate',function(){
    var t=v.currentTime,cur=null;
    btns.forEach(function(b){if(parseFloat(b.dataset.t)<=t+0.5)cur=b;});
    btns.forEach(function(b){b.classList.toggle('on',b===cur);});
  });
  // allow ep page links like #t=120
  var m=location.hash.match(/t=(\d+)/); if(m){v.addEventListener('loadedmetadata',function(){v.currentTime=+m[1];},{once:true});}
})();
