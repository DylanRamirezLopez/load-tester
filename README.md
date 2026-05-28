# ⚡ Load Tester

**Load Tester** es una herramienta web de pruebas de carga basada en [k6](https://k6.io/), con interfaz visual en Flask. Permite simular múltiples usuarios concurrentes contra cualquier servidor web, API REST, WebSocket, gRPC o sitio web completo para medir su comportamiento bajo estrés.

> **⚠️ Aviso importante:** Esta herramienta fue creada con fines de **análisis y hardening de sistemas propios**. El uso de esta herramienta contra sistemas sin autorización expresa puede violar leyes locales e internacionales. El autor no se responsabiliza por el mal uso que se le pueda dar. Úsala responsablemente y solo en infraestructura que te pertenezca o tengas permiso explícito para probar.

---

## 📋 ¿Para qué fue creado?

Este proyecto nació para **evaluar la resistencia y estabilidad de servidores web y APIs** ante diferentes patrones de carga. Está diseñado para:

- Equipos de desarrollo que necesitan saber si su backend soporta el tráfico esperado
- Ingenieros de plataforma que hacen hardening de infraestructura
- Pruebas de capacidad antes de lanzar un producto a producción
- Identificar cuellos de botella y puntos de quiebre en sistemas web
- Comparar el rendimiento entre dos versiones o configuraciones del mismo servicio

No fue creado para actividades maliciosas. Si lo usas para hacer daño, el problema es tuyo, no mío.

---

## ✨ Características

### Core
- **5 tipos de prueba clásicos:** Login, Subida de archivos, Ambos, Genérico (cualquier endpoint), Desde HAR
- **4 tipos avanzados:** WebSocket, gRPC, Browser (k6), Estrés progresivo
- **Modo distribuido:** Divide usuarios entre N instancias de k6 en paralelo
- **Comparativa lado a lado:** Prueba dos URLs simultáneamente y compara resultados
- **Etapas configurables:** Ramp-up, sostenido y ramp-down con duración ajustable
- **Headers personalizados** por tipo de petición
- **Thresholds por endpoint** vía JSON configurable
- **Métricas en tiempo real:** Requests, p95, avg, min, max, med, p50, p75, p90, p99, req/s, tasa de fallo

### Visualización
- **Gráficas en vivo con Chart.js** (duración, req/s, fallos)
- **Tabla de percentiles** completa (p50, p75, p90, p95, p99)
- **Exportación:** JSON, CSV y **PDF** con resumen
- **Log de respuestas fallidas** para debuggear

### Automatización
- **Tareas programadas** (schedules) con intervalo configurable
- **Webhooks** para notificaciones Slack/Discord/email
- **CLI mode** para integración en scripts y CI/CD
- **API REST documentada** con OpenAPI/Swagger
- **Docker** listo para entornos containerizados

### UX
- Modo oscuro / claro con persistencia
- Secciones colapsables
- Atajos de teclado (Ctrl+Enter)
- Tooltips explicativos en cada campo
- Re-ejecutar tests desde el historial con un clic

---

## 🧱 Arquitectura

```
┌─────────────────────────────────────┐
│         Navegador (HTML/JS)         │
│  Chart.js · html2canvas · jsPDF     │
└──────────────┬──────────────────────┘
               │ HTTP / SSE
┌──────────────▼──────────────────────┐
│     Flask Backend (Python)          │
│  · API REST · Scheduler · Webhooks  │
│  · Generación scripts k6            │
│  · Agregación de resultados         │
│  · Persistencia JSON                │
└──────────────┬──────────────────────┘
               │ subprocess
┌──────────────▼──────────────────────┐
│        k6 (motor de carga)          │
│  · HTTP/1.1 · HTTP/2 · WebSocket    │
│  · gRPC · Browser (chromium)        │
└─────────────────────────────────────┘
```

**Flujo de una prueba:**
1. El usuario configura la prueba en el navegador y la envía
2. Flask recibe la configuración, genera un script de k6 con los parámetros
3. Ejecuta k6 como subproceso con `--out json` para métricas en tiempo real
4. Un hilo lee el NDJSON y actualiza métricas intermedias cada 2 segundos
5. El frontend poll/stream los resultados y actualiza gráficas en vivo
6. Al terminar, k6 genera un summary JSON que se agrega, persiste y muestra

---

## 🚀 Instalación y ejecución

### Requisitos
- **Python 3.8+**
- **k6** ([descargar](https://k6.io/docs/getting-started/installation/))
- Windows, Linux o macOS

### Instalación local

```bash
# Clonar
git clone https://github.com/TU-USUARIO/load-tester.git
cd load-tester

# Instalar dependencias Python
pip install -r requirements.txt

# Configurar ruta de k6 (opcional, por defecto busca en C:\Program Files\k6\k6.exe en Windows)
# En Linux/Mac:
export K6_PATH=/usr/bin/k6

# Ejecutar
python server.py
```

Abrir navegador en `http://127.0.0.1:5000`

### Con Docker

```bash
docker build -t load-tester .
docker run -p 5000:5000 load-tester
```

### Variables de entorno

| Variable | Default | Descripción |
|----------|---------|-------------|
| `K6_PATH` | `C:\Program Files\k6\k6.exe` | Ruta al ejecutable de k6 |
| `MAX_CONCURRENT_TESTS` | `10` | Máximo de pruebas simultáneas |
| `MAX_SCRIPT_AGE_DAYS` | `7` | Días antes de limpiar scripts temporales |

---

## 🖥️ CLI mode

Ejecutar pruebas desde terminal sin abrir el navegador:

```bash
python server.py --cli --url https://miapi.com/login --type login --users 100 --output json
```

Opciones:
- `--url` URL objetivo (requerido)
- `--type` Tipo de prueba (default: login)
- `--users` Usuarios simultáneos (default: 10)
- `--output` Formato de salida: json | text (default: json)

---

## 📡 API REST

La API está documentada con Swagger en `/apidocs/` o en formato OpenAPI en `/api/docs`.

### Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/api/status` | Estado del servidor y k6 |
| `POST` | `/api/run` | Ejecutar prueba de carga |
| `POST` | `/api/cancel/<id>` | Cancelar prueba en curso |
| `GET` | `/api/results/<id>` | Resultados de una prueba |
| `GET` | `/api/stream/<id>` | SSE en tiempo real |
| `GET` | `/api/history` | Historial de pruebas |
| `GET` | `/api/export/<id>/json` | Exportar JSON |
| `GET` | `/api/export/<id>/csv` | Exportar CSV |
| `GET` | `/api/responses/<id>` | Log de respuestas fallidas |
| `POST` | `/api/cleanup` | Limpiar archivos temporales |
| `POST` | `/api/har/parse` | Parsear archivo HAR |
| `GET/POST` | `/api/schedules` | Gestionar tareas programadas |
| `DELETE` | `/api/schedules/<id>` | Eliminar tarea programada |
| `GET/POST` | `/api/webhooks` | Gestionar webhooks |
| `DELETE` | `/api/webhooks/<id>` | Eliminar webhook |
| `POST` | `/api/comparison` | Iniciar comparativa |
| `GET` | `/api/comparison/<id>` | Resultados de comparativa |
| `GET` | `/api/docs` | Documentación OpenAPI |

---

## 🧪 Tipos de prueba

### Login
Simula inicios de sesión contra un endpoint POST. Configurable: endpoint, credenciales, nombres de campo, headers.

### Subida de archivo
Simula subida de archivos binarios. Configurable: método (POST/PUT), tamaño del archivo, campo, headers.

### Ambos
Ejecuta login + subida secuencialmente por cada usuario virtual.

### Genérico
Peticiones HTTP con método, path, headers y body personalizados. Soporta GET, POST, PUT, PATCH, DELETE. Se pueden definir múltiples peticiones y cada VU elige una aleatoriamente.

### WebSocket
Conecta a un endpoint WebSocket, envía un mensaje y espera respuesta. Ideal para probar chats, notificaciones en tiempo real, etc.

### gRPC
Realiza llamadas a servicios gRPC. Requiere: address (host:puerto), proto file, servicio y método.

### Browser
Usa k6 browser (chromium) para navegar páginas como un usuario real. Mide rendimiento de frontend bajo carga.

### Estrés progresivo
Escalona usuarios progresivamente (5→10→20→40→80→160→...) hasta encontrar el punto de quiebre. Cada escalón duplica la carga.

### Desde HAR
Importa una grabación de navegador (archivo .har) y reproduce las peticiones bajo carga.

---

## 📁 Estructura del proyecto

```
load-tester/
├── server.py              # Servidor Flask (todo el backend)
├── templates/index.html   # Frontend (SPA)
├── requirements.txt       # Dependencias Python
├── Dockerfile             # Imagen Docker
├── launch.ps1             # Script de inicio (Windows)
├── README.md              # Este archivo
├── LICENSE                # Licencia MIT
├── scripts/               # Scripts k6 temporales
├── results/               # Resultados temporales (JSON, NDJSON)
├── history/               # Historial persistente de pruebas
├── responses/             # Log de respuestas fallidas
└── static/                # Archivos estáticos (futuro)
```

---

## 📄 Licencia

Este proyecto está bajo la licencia MIT. Ver archivo [LICENSE](LICENSE).

**Uso responsable:** Esta herramienta fue creada para análisis y hardening de sistemas propios. No me hago responsable del uso que terceros puedan darle. Usar esta herramienta contra sistemas sin autorización es ilegal y poco ético.

---

## 🤝 Contribuciones

Si encuentras un bug o quieres sugerir una mejora, abre un issue o un PR. Este proyecto se mantiene por diversión y necesidad personal, pero las contribuciones son bienvenidas.
