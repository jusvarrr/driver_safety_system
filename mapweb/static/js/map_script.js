const style = document.createElement('style');
style.innerHTML = `
  .control-container {
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 10;
    display: flex;
    flex-direction: column;
    gap: 5px;
  }
  .system-status-panel {
    padding: 10px;
    background-color: rgba(44, 62, 80, 0.9);
    color: #ffffff;
    border-radius: 4px;
    font-size: 12px;
    font-family: sans-serif;
    display: flex;
    flex-direction: column;
    gap: 5px;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
  }
  .status-header {
    font-weight: bold;
    border-bottom: 1px solid #7f8c8d;
    padding-bottom: 3px;
    margin-bottom: 3px;
  }
  .status-nav-text {
    color: #c4c4c4;
    font-weight: bold;
  }
  .nav-btn {
    padding: 8px 12px;
    background-color: #ffffff;
    color: #000000;
    border: 1px solid #ccc;
    border-radius: 4px;
    cursor: pointer;
    box-shadow: 0 2px 4px rgba(0,0,0,0.2);
  }
  .btn-blue {
    background-color: #3498db;
    color: #ffffff;
  }
  .btn-red {
    background-color: #e74c3c;
    color: #ffffff;
  }
  .btn-yellow {
    background-color: #f1c40f;
    color: #000000;
  }
  .rotation-slider {
    width: 100%;
    margin-top: 4px;
  }
`;
document.head.appendChild(style);

const map = new maplibregl.Map({
  style: "http://10.3.141.1:5000/style.json",
  container: 'pymaplibregl',
  center: [23.9, 54.9],
  zoom: 6,
});

const arrowEl = document.createElement('div');
arrowEl.className = 'vehicle-arrow-marker';
arrowEl.innerHTML = `
  <svg width="40" height="40" viewBox="0 0 100 100" style="display: block;">
    <polygon points="50,15 20,85 50,65 80,85" fill="#E74C3C" stroke="#FFFFFF" stroke-width="6" stroke-linejoin="round"/>
  </svg>
`;

let vehicleMarker = null;
let correctionStep = 0; 
let tempLat = null;
let tempLon = null;
let tempCourse = 0;
let rotationSlider = null;

const allMarkers = [];
let marksVisible = true;

const socket = new WebSocket('ws://' + window.location.hostname + ':5000/ws');

const statusPanel = document.createElement('div');
statusPanel.className = 'system-status-panel';
statusPanel.innerHTML = `
  <div class="status-header">System info</div>
  <div>Navigation mode: <span id="status-nav" class="status-nav-text">Waiting for GNSS signal</span></div>
`;

socket.onopen = () => {
  console.log("WebSocket connected.");
};

socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      const topic = msg.topic;
      const payload = msg.data;

      if (!payload) return;

      if (topic === 'marks/local' || topic === 'marks/cloud' || topic === 'button/loc') {
        addMarkToMap(payload);
      }
      else if (topic === 'db/response' && msg.req_id === 'init_marks') {
        if (Array.isArray(payload)) {
          payload.forEach(mark => {
            addMarkToMap(mark);
          });
        }
      }
      else if (topic === 'location/gnss' || topic === 'location/dr') {
        const navStatusEl = document.getElementById('status-nav');
        if (topic === 'location/dr') {
          navStatusEl.innerText = "Dead Reckoning on";
          navStatusEl.style.color = '#e67e22';
        } else {
          navStatusEl.innerText = "GNSS Stable";
          navStatusEl.style.color = '#2ecc71';
        }

        if (correctionStep > 0) return;

        console.log("GPS PAYLOAD:", payload);

        const lat = parseFloat(payload.lat);
        const lon = parseFloat(payload.lon);
        console.log("PARSED:", lat, lon);
        if (isNaN(lat) || isNaN(lon)) {
          console.warn("Invalid coordinates");
          return;
        }

        if (!vehicleMarker) {
          vehicleMarker = new maplibregl.Marker({
            element: arrowEl,
            rotationAlignment: 'map'
          })
            .setLngLat([lon, lat])
            .addTo(map);

          console.log("Vehicle marker initialized.");
        } else {
          vehicleMarker.setLngLat([lon, lat]);
        }

        if (payload.course !== undefined) {
          vehicleMarker.setRotation(payload.course);
        }

        map.easeTo({
          center: [lon, lat],
          duration: 500
        });
      }
    } catch (err) {
      console.error("Error in WS message:", err);
    }
  };

map.on('load', () => {
  console.log("Map loaded");
});

socket.onclose = () => {
    console.log("Disconnected and reconnecting...");
    setTimeout(connectWS, 2000);
};

map.addControl(new maplibregl.NavigationControl());

function addMarkToMap(mark) {
  if (typeof mark === 'string') {
    try {
      mark = JSON.parse(mark);
    } catch (e) {
      console.error("Failed to parse double serialized mark string:", e);
      return;
    }
  }
  let color = '#3fb1ce';
  if (mark.type === 'markedImportant') color = '#f1c40f';
  if (mark.type === 'markedDangerous') color = '#5a231d';

  const lon = parseFloat(mark.lon || mark.long || mark.longitude);
  const lat = parseFloat(mark.lat);

  if (!isNaN(lon) && !isNaN(lat)) {
    const newMarker = new maplibregl.Marker({ color: color })
      .setLngLat([lon, lat])
      .setPopup(new maplibregl.Popup().setHTML(`
        <strong>${mark.name || 'Emergency Mark'}</strong><br>
        Type: ${mark.type || 'unclassified'}<br>
        Info: ${mark.info || ''}
      `));

    if (marksVisible) {
      newMarker.addTo(map);
    }

    allMarkers.push(newMarker);
    console.log(`Mark [${mark.name || 'Unnamed'}] loaded successfully.`);
  } else {
    console.warn("Mark missing or has invalid coordinates:", mark);
  }
}

map.on('click', (e) => {
  if (correctionStep > 0) return;
  const name = prompt("Name:");
  if (!name) return;
  const info = prompt("Info:");
  const type = prompt("Type (markedImportant, markedDangerous, unclassified):", "unclassified");
  const sendToCloud = confirm("Send to cloud via mobile network?");

  const markData = {
    name: name,
    lon: e.lngLat.lng,
    lat: e.lngLat.lat,
    info: info,
    type: type,
    sync_cloud: sendToCloud
  };

  console.log("Post data (mark):", markData);

  fetch('/mark', {
    method: 'POST',
    body: JSON.stringify(markData),
    headers: { 'Content-Type': 'application/json' }
  })
  .then(response => response.json())
  .then(res => {
    if (res.status === "ok") {
      console.log("Flask sent to UDS hub.");
    }
  })
  .catch(err => console.error("Error in request:", err));
});

const controlContainer = document.createElement('div');
controlContainer.className = 'control-container';
document.getElementById('pymaplibregl').appendChild(controlContainer);

const toggleMarksBtn = document.createElement('button');
toggleMarksBtn.className = 'nav-btn';
toggleMarksBtn.innerText = 'Hide/Show Marks';

toggleMarksBtn.onclick = () => {
  marksVisible = !marksVisible;
  
  allMarkers.forEach(m => {
    if (marksVisible) {
      m.addTo(map);
    } else {
      m.remove();
    }
  });
  
  console.log(`Marks visibility changed. Visible: ${marksVisible}`);
};

const manualCorrectionOffBtn = document.createElement('button');
manualCorrectionOffBtn.className = 'nav-btn';
manualCorrectionOffBtn.innerText = 'Manual Correction Off';
manualCorrectionOffBtn.style.visibility = 'hidden';

manualCorrectionOffBtn.onclick = () => {
  fetch('/off_manual_correct', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' }
  })
  .then(response => response.json())
  .then(res => {
    if (res.status === "ok") {
      console.log("Flask manual correction off sent to UDS hub.");
      manualCorrectionOffBtn.style.visibility = 'hidden';
    }
  })
  .catch(err => console.error("Error in request:", err));
};

controlContainer.appendChild(statusPanel);
controlContainer.appendChild(toggleMarksBtn);

const manualCorrectionBtn = document.createElement('button');
manualCorrectionBtn.className = 'nav-btn';
manualCorrectionBtn.innerText = 'Manual Correction';

manualCorrectionBtn.onclick = () => {
  if (!vehicleMarker) {
    const center = map.getCenter();
    vehicleMarker = new maplibregl.Marker({ element: arrowEl, rotationAlignment: 'map' })
      .setLngLat([center.lng, center.lat])
      .addTo(map);
  }

  if (correctionStep === 0) {
    correctionStep = 1;
    manualCorrectionBtn.innerText = 'Confirm Coordinates';
    manualCorrectionBtn.className = 'nav-btn btn-red';

    vehicleMarker.setDraggable(true);
    console.log("Location correction activated. Drag the red marker to your wanted position.");

  } else if (correctionStep === 1) {
    const lngLat = vehicleMarker.getLngLat();
    tempLon = lngLat.lng;
    tempLat = lngLat.lat;
    
    vehicleMarker.setDraggable(false);

    correctionStep = 2;
    manualCorrectionBtn.innerText = 'Confirm Orientation';
    manualCorrectionBtn.className = 'nav-btn btn-yellow';

    rotationSlider = document.createElement('input');
    rotationSlider.type = 'range';
    rotationSlider.min = '0';
    rotationSlider.max = '360';
    rotationSlider.className = 'rotation-slider';
    rotationSlider.value = vehicleMarker.getRotation() || '0';
    
    rotationSlider.oninput = (e) => {
      tempCourse = parseFloat(e.target.value);
      vehicleMarker.setRotation(tempCourse);
    };
    
    controlContainer.appendChild(rotationSlider);
    console.log("Orientation correction activated. Spin the slider to adjust heading.");

  } else if (correctionStep === 2) {
    correctionStep = 0;
    manualCorrectionBtn.innerText = 'Manual Correction';
    manualCorrectionBtn.className = 'nav-btn';

    if (rotationSlider) {
      rotationSlider.remove();
      rotationSlider = null;
    }

    const correctionPayload = {
      lon: tempLon,
      lat: tempLat,
      orient_offset: tempCourse.toString()
    };

    console.log("Manual corrections to server:", correctionPayload);

    fetch('/manual_correct', {
      method: 'POST',
      body: JSON.stringify(correctionPayload),
      headers: { 'Content-Type': 'application/json' }
    })
    .then(response => response.json())
    .then(res => {
      console.log("Processed change status successfully:", res);
      manualCorrectionOffBtn.style.visibility = 'visible';
    })
    .catch(err => console.error("Error sending manual adjustments:", err));
  }
};

controlContainer.appendChild(manualCorrectionBtn);

const modeControlContainer = document.createElement('div');
modeControlContainer.style.position = 'absolute';
modeControlContainer.style.top = '10px';
modeControlContainer.style.left = '10px';
modeControlContainer.style.zIndex = '1000';

controlContainer.appendChild(modeControlContainer);

const sendCurrentLocBtn = document.createElement('button');
sendCurrentLocBtn.className = 'nav-btn btn-blue';
sendCurrentLocBtn.innerText = 'Send Current Location';

sendCurrentLocBtn.onclick = () => {
    if (!vehicleMarker) {
        alert("Lokacija dar nenustatyta!");
        return;
    }
    const pos = vehicleMarker.getLngLat();
    const markData = {
        name: "Quick Loc. Share",
        lon: pos.lng,
        lat: pos.lat,
        info: "Quick Share from the driver",
        type: "markedEmergency",
        sync_cloud: true
    };

    fetch('/mark', {
        method: 'POST',
        body: JSON.stringify(markData),
        headers: { 'Content-Type': 'application/json' }
    }).then(r => console.log("Current location sent!"));
};

controlContainer.appendChild(sendCurrentLocBtn);
controlContainer.appendChild(manualCorrectionOffBtn);