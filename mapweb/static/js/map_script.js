const map = new maplibregl.Map({
  style: "http://10.3.141.1:5000/style.json",
  container: 'pymaplibregl',
  center: [23.9, 54.9],
  zoom: 6,
});

let vehicleMarker = null;

let correctionStep = 0; // 0 = not enabled, 1 = coordinates, 2 = rotation
let tempLat = null;
let tempLon = null;
let tempCourse = 0;
let rotationSlider = null;

const allMarkers = [];
let marksVisible = true;

const socket = new WebSocket('ws://' + window.location.hostname + ':5000/ws');

socket.onopen = () => {
  console.log("WebSocket connected.");
};

map.on('load', () => {
  socket.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      const topic = msg.topic;
      const payload = msg.data;

      if (!payload) return;

      if (topic === 'marks/local' || topic === 'marks/cloud' || topic === 'button/loc') {
        addMarkToMap(payload);
      }
      else if (topic === 'location/gnss' || topic === 'location/dr') {
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
            color: "#FF0000"
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
});

socket.onclose = () => {
    console.log("Disconnected and reconnecting...");
};

map.addControl(new maplibregl.NavigationControl());

function addMarkToMap(mark) {
  let color = '#3fb1ce';
  if (mark.type === 'markedImportant') color = '#f1c40f';
  if (mark.type === 'markedDangerous') color = '#e74c3c';

  if (mark.lon && mark.lat) {
    const newMarker = new maplibregl.Marker({ color: color })
      .setLngLat([mark.lon, mark.lat])
      .setPopup(new maplibregl.Popup().setHTML(`
        <strong>${mark.name || 'Emergency Mark'}</strong><br>
        Type: ${mark.type}<br>
        Info: ${mark.info || ''}
      `));

    if (marksVisible) {
      newMarker.addTo(map);
    }

    allMarkers.push(newMarker);
    console.log(`Mark [${mark.name}] success.`);
  } else {
    console.warn("Mark missing coordinates:", mark);
  }
}

map.on('click', (e) => {
  if (correctionStep > 0) return;
  const name = prompt("Name:");
  if (!name) return;
  const info = prompt("Info:");
  const type = prompt("Type (markedImportant, markedDangerous, unclassified):", "unclassified");

  const markData = {
    name: name,
    lon: e.lngLat.lng,
    lat: e.lngLat.lat,
    info: info,
    type: type
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
controlContainer.style.position = 'absolute';
controlContainer.style.top = '10px';
controlContainer.style.right = '10px';
controlContainer.style.zIndex = '10';
controlContainer.style.display = 'flex';
controlContainer.style.flexDirection = 'column';
controlContainer.style.gap = '5px';
document.getElementById('pymaplibregl').appendChild(controlContainer);

const toggleMarksBtn = document.createElement('button');
toggleMarksBtn.innerText = 'Hide/Show Marks';
toggleMarksBtn.style.padding = '8px 12px';
toggleMarksBtn.style.backgroundColor = '#ffffff';
toggleMarksBtn.style.border = '1px solid #ccc';
toggleMarksBtn.style.borderRadius = '4px';
toggleMarksBtn.style.cursor = 'pointer';
toggleMarksBtn.style.boxShadow = '0 2px 4px rgba(0,0,0,0.2)';

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
controlContainer.appendChild(toggleMarksBtn);

const manualCorrectionBtn = document.createElement('button');
manualCorrectionBtn.innerText = 'Manual Correction';
manualCorrectionBtn.style.padding = '8px 12px';
manualCorrectionBtn.style.backgroundColor = '#ffffff';
manualCorrectionBtn.style.border = '1px solid #ccc';
manualCorrectionBtn.style.borderRadius = '4px';
manualCorrectionBtn.style.cursor = 'pointer';
manualCorrectionBtn.style.boxShadow = '0 2px 4px rgba(0,0,0,0.2)';

manualCorrectionBtn.onclick = () => {
  if (!vehicleMarker) {
    const center = map.getCenter();
    vehicleMarker = new maplibregl.Marker({ color: "#FF0000" })
      .setLngLat([center.lng, center.lat])
      .addTo(map);
  }

  if (correctionStep === 0) {
    correctionStep = 1;
    manualCorrectionBtn.innerText = 'Confirm Coordinates';
    manualCorrectionBtn.style.backgroundColor = '#e74c3c';
    manualCorrectionBtn.style.color = '#ffffff';

    vehicleMarker.setDraggable(true);
    console.log("Location correction activated. Drag the red marker to your wanted position.");

  } else if (correctionStep === 1) {
    const lngLat = vehicleMarker.getLngLat();
    tempLon = lngLat.lng;
    tempLat = lngLat.lat;
    
    vehicleMarker.setDraggable(false);

    correctionStep = 2;
    manualCorrectionBtn.innerText = 'Confirm Orientation';
    manualCorrectionBtn.style.backgroundColor = '#f1c40f';
    manualCorrectionBtn.style.color = '#000000';

    rotationSlider = document.createElement('input');
    rotationSlider.type = 'range';
    rotationSlider.min = '0';
    rotationSlider.max = '360';
    rotationSlider.value = vehicleMarker.getRotation() || '0';
    rotationSlider.style.width = '100%';
    rotationSlider.style.marginTop = '4px';
    
    rotationSlider.oninput = (e) => {
      tempCourse = parseFloat(e.target.value);
      vehicleMarker.setRotation(tempCourse);
    };
    
    controlContainer.appendChild(rotationSlider);
    console.log("Orientation correction activated. Spin the slider to adjust heading.");

  } else if (correctionStep === 2) {
    correctionStep = 0;
    manualCorrectionBtn.innerText = 'Manual Correction';
    manualCorrectionBtn.style.backgroundColor = '#ffffff';
    manualCorrectionBtn.style.color = '#000000';

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
    })
    .catch(err => console.error("Error sending manual adjustments:", err));
  }
};
controlContainer.appendChild(manualCorrectionBtn);