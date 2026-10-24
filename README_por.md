<!-- =============================================================================
HYDRA-UMC-BRIDGE-CNC - CNC cell auxiliary coordination bridge
Copyright (C) 2026 JuanenRac (Electro Hobby 3D) <electrohobby3d@gmail.com>
GPL-3.0-or-later - see LICENSE
============================================================================= -->

<p align="center">
  <img src="images/HYDRA_UMC_BANNER.svg" alt="HYDRA-UMC-BRIDGE-CNC banner" width="100%">
</p>

# 🔧 HYDRA-UMC-BRIDGE-CNC

<p align="center"><a href="README.md">🇺🇸 English</a> | <a href="README_spa.md">🇪🇸 Español</a> | <a href="README_fra.md">🇫🇷 Français</a> | <a href="README_ita.md">🇮🇹 Italiano</a> | <a href="README_deu.md">🇩🇪 Deutsch</a> | <a href="README_zho.md">🇨🇳 简体中文</a> | <a href="README_jpn.md">🇯🇵 日本語</a> | 🇵🇹 <b>Português</b></p>

### 🛑 Bridge de coordenação de falha segura para células CNC

<p align="center">
  <img src="https://img.shields.io/badge/License-GPL%203.0--or--later-blue.svg" alt="GPL 3.0 or later">
  <img src="https://img.shields.io/badge/Python-3.11%2B-3776AB.svg" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/Safety-Fails%20Closed-red.svg" alt="Fails Closed">
</p>

---

> **Verificação de honestidade - o que realmente corre hoje:** o instantâneo de célula com falha segura e a porta de segurança (o `CncSnapshot`/`CncCellBridge` de `cell.py`, que encaminha cada trabalho pelo `evaluate_job()` do próprio `HYDRA-UMC-SDK`), o normalizador só de leitura de evidência GRBL/MTConnect (`observation.py`), o transporte série GRBL real (o `GrblSerialProbe`/`GrblRealtimeControl` de `serial_transport.py`) e o transporte MQTT de comandos/estado (`mqtt_transport.py`) são reais e estão cobertos por 206 casos `unittest` que passam (`python tools/build_test.py`), incluindo `tests/test_grbl_emulator.py`, que executa este bridge contra um emulador GRBL v1.1 escrito à mão e fiel ao protocolo. Nada disto tocou numa porta série real, num broker MQTT real nem num controlador CNC verdadeiro: o `FakeSerial` de `test_serial_transport.py` substitui uma ligação física, e `test_mqtt_transport.py` usa um cliente de broker falso do mesmo modo. As ligações ao LinuxCNC e ao CNCjs e a vista MTConnect só conheceram simulações, não há adaptador HAL do LinuxCNC nem comando `run` vivo, porque nenhum controlador real foi acionado; ver «Estado atual e próximos passos» abaixo, que já o diz com clareza, e `CHANGELOG.md` para saber exatamente o que foi entregue até agora.

---

> **Estado de segurança (safety e security):** este ecossistema não é um sistema de segurança certificado. Ainda não existe um caminho de E-STOP por hardware, nem limites ou interlocks impostos pelo firmware, nem um comando de movimento verificado; as imagens de firmware são verificadas com CRC32 e um HMAC-SHA256 de chave partilhada, não com uma assinatura assimétrica; TLS e TLS mútuo são opcionais e estão desativados por defeito; nenhuma placa própria foi ainda montada ou testada. Leia a [base de segurança](https://github.com/JuanenRac/HYDRA-UMC-DOCS/blob/main/docs/SECURITY_BASELINE.md) e o [modelo de ameaças](https://github.com/JuanenRac/HYDRA-UMC-DOCS/blob/main/docs/THREAT_MODEL.md) antes de confiar em qualquer afirmação aqui para a segurança de uma pessoa.

## 📋 Num relance

| | |
|---|---|
| Versão | `0.1.9` |
| Maturidade | Funcional |
| Testes | 206 (`python tools/build_test.py`) |
| Tecnologia | Python / LinuxCNC / G-code / GRBL status evidence / GRBL serial transport |
| Destino | O Raspberry Pi CM5 de uma célula HYDRA-UMC |
| Em hardware real | Só simulado: testado com emuladores, dublês de teste ou simuladores; não há registo de execução em hardware real para esta versão |

## 1. 🛠️ VISÃO TÉCNICA GERAL

O **HYDRA-UMC-BRIDGE-CNC** é o bridge de alto nível entre as células CNC e os auxiliares robóticos do HYDRA-UMC: carga, descarga, manuseamento de peças e tarefas auxiliares supervisionadas. Nunca se torna um controlador de trajetórias em tempo real: o controlador CNC (GRBL, LinuxCNC ou o que está por trás do CNCjs) mantém em todo o momento a autoridade sobre a trajetória, o fuso e os limites da máquina.

Pertence à família de **Bridges de automação externa**: um conjunto de repositórios irmãos (CNC, LASER, OPENPNP, PRINTER3D, ROS2) que falam todos o mesmo contrato de segurança partilhado do `HYDRA-UMC-SDK`, de modo que nenhum bridge possa inventar a sua própria definição de «seguro para trabalhar».

### Funcionalidades principais:
* ✅ **Instantâneo real de célula com falha segura:** o `CncSnapshot.machine_state()` de `cell.py` verifica a paragem de emergência e o estado da porta *antes* de olhar para qualquer coisa que o controlador comunique: uma paragem de emergência ativada ou uma porta aberta resolvem-se sempre em `SAFE_STOP`, diga o que disser `controller_state`. *(implementado, testado em `tests/test_cell.py`)*
* ✅ **Porta de segurança partilhada real:** cada trabalho observado é reavaliado pelo `evaluate_job()` do `bridge_contract` do `HYDRA-UMC-SDK`, a mesma porta que usam todos os bridges irmãos. *(implementado)*
* ✅ **Mapeamento de estados conservador:** só `IDLE` se trata como inativo; `RUN`/`RUNNING`/`JOG`/`HOME` correspondem a `RUNNING`, `FAULT`/`ERROR`/`OFF`/`ALARM` correspondem a `FAULT` e tudo o que não se reconhece volta a `OFFLINE`, nunca a um estado que permitiria trabalho produtivo. (`HOLD` → `HOLDING`; `DOOR` → `SAFE_STOP`) *(implementado)*
* ✅ **Evidência só de leitura do controlador:** `observation.py` normaliza de forma estrita a evidência de mapeamento local e as linhas de estado GRBL fornecidas; sinais de paragem de emergência/porta omitidos ou não booleanos falham de forma segura. *(implementado, testado em `tests/test_observation.py`)*
* ✅ **Transporte série GRBL real:** o `GrblSerialProbe` de `serial_transport.py` abre uma ligação série real e consulta o byte de estado em tempo real do GRBL (`?`); o `GrblRealtimeControl` envia apenas os bytes de controlo em tempo real próprios do GRBL (retenção de avanço, retoma de ciclo, reinício suave): `cycle_start_resume()` só se permite com uma máquina genuinamente em `HOLDING` e os outros permitem-se sempre, como `ABORT`. Nunca transmite um programa de G-code. *(implementado, testado em `tests/test_serial_transport.py`)*
* ✅ **Compilação/teste que não altera nada:** `build-test.bat`/`.sh` compilam o código-fonte e executam a suíte determinista da porta de segurança sem tocar nos ficheiros de versão nem no CHANGELOG. *(implementado, ver COMPILAR E EXECUTAR abaixo)*
* ✅ **Ligações a controladores:** o bridge fala com um controlador através de `ControllerLink` (`controller.py`): GRBL numa porta série que o bridge mantém aberta (`GrblController`), uma instalação LinuxCNC (`linuxcnc_link.py`: lê `linuxcnc.stat`, envia apenas pausa, aborto e retoma de um programa em pausa através de `linuxcnc.command`, nunca executa um programa) e um servidor CNCjs que possui a porta (`cncjs_link.py`: lê o `controller:state` que ele difunde e envia apenas `feedhold`, `reset` e `cyclestart`, pelo que o bridge trabalha ao lado do emissor que executa os programas). Retomar exige sempre uma célula READY e uma máquina realmente em retenção; uma leitura antiga ou em falta nunca é uma máquina em repouso. Ambas as ligações são testadas contra simulações (`SimulatedLinuxcnc`, `SimulatedCncjs`); nenhuma tocou num LinuxCNC ou CNCjs reais. *(implementado, apenas simulado)*
* ✅ **Porta do fuso:** recusa-se uma carga ou uma descarga quando o controlador está em repouso mas ainda indica um fuso a rodar ou um avanço em movimento (`FS:`). `HYDRA_UMC_CNC_SPINDLE_GATE` vale `off`, `when_known` (por omissão: recusa o que é indicado) ou `required` (recusa também o que o controlador não indica). O instantâneo traz ainda as posições, o desvio de trabalho e os overrides do relatório. *(implementado)*
* ✅ **Vista MTConnect:** `mtconnect_publish.py` converte o instantâneo em observações MTConnect (`avail`, `execution`, `estop`, `door`, `spindle_speed`, `path_feedrate`, `system`), servidas como SHDR por TCP pelo `ShdrAdapter` (só de leitura; a única linha que aceita é `* PING`) e, com `HYDRA_UMC_CNC_MTCONNECT=1`, publicadas retidas em `hydra/bridges/cnc/mtconnect`. Um controlador que não responde é `UNAVAILABLE`, nunca `READY`. Testado contra um agente simulado. *(implementado, ainda não experimentado com um agente real)*
* 🔜 **Pinos HAL do LinuxCNC:** adiado; ainda não se acionou nenhum controlador CNC, pino HAL ou robô reais. *(planeado)*
* ✅ **Chaves de idempotência repetidas:** `CncMqttBridge` aceita um `IdempotencyStore`: uma chave de trabalho repetida recebe a resposta dada da primeira vez em vez de ser avaliada de novo. *(implementado, testado em `tests/test_idempotency.py`)*
* ✅ **Respostas de estado alinhadas, série com falha segura, ciclo MQTT blindado:** a consulta de estado descarta o que já esperava, envia `?` e lê até chegar um relatório de estado real (`<...>`): o banner, `ok`, `[MSG:...]` e `ALARM:n` que o GRBL envia sem que lhos peçam deixam de atrasar cada consulta seguinte uma resposta; um dispositivo USB em falta (ou qualquer falha da ligação) falha de forma segura em vez de terminar o bridge, e a porta é aberta sem ativar o DTR; `cmd/feed_hold` indica o que o controlador disse depois (`controller_state_after`, `confirmed`), e `cmd/feed_hold` e `cmd/soft_reset` correm numa faixa própria, pelo que uma consulta de estado nunca os atrasa; uma carga demasiado grande (mais de 16 KiB), ilegível ou com JSON profundamente aninhado, e qualquer falha inesperada, recebem uma recusa no tópico de resultado e nunca terminam o ciclo; as respostas de `cmd/job` indicam `"mode": "gate-only"` e um ABORT por aí diz que nada foi parado; o estado inclui `observed_at` e o bridge anuncia-se num tópico `availability` com Last Will. O GRBL tem UMA só porta série: enquanto este bridge a mantiver aberta, nenhum emissor de G-code a pode usar (ver `docs/BRIDGE_GUIDE.md`). *(implementado, testado em `tests/test_hardening.py`)*

---

## 2. 🔄 FLUXO DE COORDENAÇÃO DA CÉLULA

```mermaid
flowchart LR
    CNC["Controlador CNC / HAL<br/>(estado, E-STOP, porta)"] --> BRIDGE["BRIDGE-CNC<br/>CncSnapshot.machine_state()"]
    BRIDGE -- "BridgeJob + MachineState observado" --> SDK["HYDRA-UMC-SDK<br/>evaluate_job()"]
    SDK -- GateDecision --> RESULT["MQTT: cmd/job/result"]
```

---

## 3. 🧱 ARQUITETURA E DECISÕES DE DESENHO

* **Porque a paragem de emergência e o estado da porta se verificam antes do estado que o próprio controlador comunica.** `CncSnapshot.machine_state()` passa diretamente a `SAFE_STOP` perante `estop` ou uma porta não fechada antes de mais nada: um controlador que ainda indica `IDLE` com a porta aberta nunca deve ser levado a sério à primeira.
* **Porque o mapeamento do estado do controlador é conservador de propósito.** Só a cadeia literal `IDLE` corresponde a `MachineState.IDLE`. Todo o valor não reconhecido volta a `OFFLINE`, nunca a algo que permitisse trabalho auxiliar: um estado de controlador desconhecido trata-se como «não sei», não como «provavelmente está bem».
* **Porque o bridge constrói um novo `BridgeJob` e delega no `evaluate_job()` partilhado em vez de escrever a sua própria lógica de aceitar/rejeitar.** Os cinco Bridges de automação externa (CNC, LASER, OPENPNP, PRINTER3D, ROS2) reutilizam exatamente o mesmo `bridge_contract` do `HYDRA-UMC-SDK`, de modo que o que conta como «seguro para iniciar um trabalho» não pode divergir em silêncio entre eles.
* **Porque as ligações ao LinuxCNC e ao CNCjs apenas leem, pausam e retomam.** O controlador mantém a autoridade sobre a trajetória, o fuso e os limites, e o programa pertence a quem o executa (o CNCjs, o próprio LinuxCNC): o bridge pode levar a máquina ao repouso e deixar prosseguir uma máquina em pausa, nada mais. Ligar pinos HAL reais continua a ser um passo de integração de hardware que tem de se validar contra um controlador verdadeiro.
* **Como se encaixa no resto do ecossistema.** O BRIDGE-CNC fica entre o controlador CNC e o `HYDRA-UMC-SDK` → `HYDRA-UMC-MQTT-BROKER` → segurança do MCU/célula: coordena o trabalho auxiliar do robô em redor da CNC e não substitui nem anula a autoridade de segurança do próprio controlador.

Os auxiliares de observação são normalizadores de evidência, não adaptadores de controlador: não abrem ligações HAL, série nem de rede. O [Limite de evidência do controlador](docs/CONTROLLER_EVIDENCE_BOUNDARY.md) define a validação exigida antes de uma integração real com um controlador.

---

## 📂 ESTRUTURA DE DIRETORIAS

```text
HYDRA-UMC-BRIDGE-CNC/
├── src/
│   └── hydra_umc_bridge_cnc/
│       ├── __init__.py
│       ├── cell.py              # CncSnapshot + porta de segurança CncCellBridge
│       ├── observation.py       # Mapeamento só de leitura e normalização de estados GRBL
│       ├── serial_transport.py  # Transporte série GRBL real e com falha segura - apenas consultas de estado e bytes de controlo em tempo real (a porta e a consulta de estado são a ligação série partilhada do SDK)
│       ├── controller.py        # ControllerLink: o que o bridge precisa de qualquer controlador (+ GrblController)
│       ├── linuxcnc_link.py     # LinuxCNC: lê linuxcnc.stat, pausa, aborta, retoma (+ SimulatedLinuxcnc)
│       ├── cncjs_link.py        # CNCjs: lê a sua difusão, feedhold/reset/cyclestart (+ SimulatedCncjs)
│       ├── mtconnect_publish.py # Observações MTConnect, linhas SHDR e o adaptador SHDR só de leitura
│       └── mqtt_transport.py    # Transporte real por broker MQTT para a lógica já real deste bridge
├── tests/
│   ├── test_cell.py             # Admissão com inatividade segura, rejeição por porta aberta, encaminhamento de aborto
│   ├── test_observation.py      # A evidência de segurança em falta falha de forma segura
│   ├── test_serial_transport.py # Transporte série real contra uma porta falsa, incl. os caminhos de falha segura
│   ├── test_mqtt_transport.py   # Testes do formato de comandos/estado MQTT contra um cliente de broker falso
│   ├── test_mqtt_environment.py # As credenciais MQTT e o TLS do ambiente do serviço chegam ao cliente
│   ├── test_idempotency.py      # Uma chave de idempotência repetida é respondida uma só vez
│   ├── test_controllers.py      # Porta do fuso, ligações LinuxCNC e CNCjs contra as suas simulações, vista MTConnect
│   ├── grbl_emulator.py         # Emulador GRBL v1.1 fiel ao protocolo (dublê de teste realista)
│   ├── test_grbl_emulator.py    # Comportamento do bridge contra o emulador GRBL v1.1
│   └── test_hardening.py        # Respostas de estado alinhadas, cabo desligado, confirmação da retenção, cargas hostis
├── tools/
│   ├── build_test.py            # Executor de compilação + testes que não altera nada (build-test.bat/.sh)
│   ├── bump_version.py          # Sincroniza pyproject.toml, o manifesto e CHANGELOG.md
│   ├── ci_validate.py           # Base de CI sem dependências e não destrutiva (usada por .github/workflows/ci.yml)
│   └── inspect_controller_evidence.py # Normaliza um ficheiro JSON de evidência local guardado (sem abrir nenhuma ligação)
├── docs/
│   ├── BRIDGE_GUIDE.md                    # Âmbito, plataformas compatíveis, scripts, porta de aceitação de hardware
│   └── CONTROLLER_EVIDENCE_BOUNDARY.md    # O que conta como evidência real de segurança e o que este bridge se recusa a inferir
├── images/
│   └── HYDRA_UMC_BANNER.svg     # Banner do README
├── build-test.bat / build-test.sh  # Só valida, nunca modifica o repositório
├── build.bat / build.sh            # Valida e compila (compilar nunca altera a versão)
├── pyproject.toml               # Metadados do pacote; depende do HYDRA-UMC-SDK (git)
├── hydra-umc.project.json       # Manifesto do ecossistema (versão, maturidade, família)
├── CHANGELOG.md
├── CODE_OF_CONDUCT.md / CONTRIBUTING.md / SECURITY.md / SUPPORT.md
├── LICENSE / LICENSE.md
└── README.md / README_*.md      # Este ficheiro e as suas 7 traduções
```

---

## 4. ⚙️ COMPILAR E EXECUTAR

Requer Python 3.11+. `tools/build_test.py` espera o `HYDRA-UMC-SDK` extraído como diretoria irmã (`../HYDRA-UMC-SDK`) ou indicado pela variável de ambiente `HYDRA_UMC_SDK_ROOT`.

```bash
# Windows
build-test.bat      # só valida - sem alteração de versão/CHANGELOG
build.bat            # Valida e compila (compilar nunca altera a versão)

# Linux/macOS
bash build-test.sh
bash build.sh
```

`build-test` compila com `py_compile` cada módulo em `src/` e executa a suíte `unittest` completa descoberta em `tests/` (`test_cell.py`, `test_observation.py`, `test_serial_transport.py`, `test_mqtt_transport.py`, `test_grbl_emulator.py`, `test_hardening.py`, `test_idempotency.py`, `test_mqtt_environment.py` - 206 testes), que prova, entre tudo o resto que cobre, a admissão com inatividade segura, a rejeição por porta aberta e o encaminhamento de aborto; nunca modifica o repositório. `build` executa primeiro essa mesma validação e, só se correr bem, chama `tools/bump_version.py` para sincronizar a versão em `pyproject.toml`, `hydra-umc.project.json` e `CHANGELOG.md`. Ainda não há comando `run` de CNC viva: exige uma integração validada com um controlador.

---

## ✅ Estado atual e próximos passos

**Real hoje:** versão `0.1.9`, um coordenador de célula com falha segura testado localmente (`CncSnapshot` + `CncCellBridge`) apoiado pela porta de trabalhos partilhada do `HYDRA-UMC-SDK`, uma normalização estrita e só de leitura da evidência do controlador que cobre todo o vocabulário de estados real do GRBL v1.1, incluindo os subestados reais `Hold:N`/`Door:N` (`Idle`/`Run`/`Jog`/`Home`/`Hold`/`Alarm`/`Door`), mais os estados de execução do MTConnect, um transporte série real (`GrblSerialProbe`/`GrblRealtimeControl`) que pode consultar o estado e enviar os bytes de controlo em tempo real próprios do GRBL por uma ligação verdadeira (a leitura viva da porta/paragem de emergência faz-se agora logo a seguir à ida e volta bloqueante dessa consulta, nunca antes dela, e uma escrita real curta ou de zero bytes comunica-se como não executada e não como falso êxito), uma suíte `unittest` determinista de 206 testes e scripts build-test que não alteram nada ligados ao CI com um checkout do SDK.

**Fronteira de integração:** o controlador CNC (GRBL, LinuxCNC ou o que está por trás do CNCjs) mantém em todo o momento a autoridade sobre a trajetória, o fuso e os limites da máquina; este bridge apenas regula o trabalho robótico *auxiliar*, nunca o movimento do controlador.

**Ainda por fazer:** nenhum controlador CNC, pino HAL do LinuxCNC ou robô reais foi acionado: escolher e validar um adaptador HAL concreto é um passo de integração de hardware/software que vem depois de a máquina e a sua interface documentada estarem disponíveis.

---

## 🔗 Projetos relacionados

Este projeto faz parte do ecossistema de robótica HYDRA-UMC do mesmo autor (JuanenRac / Electro Hobby 3D). Vale a pena conhecê-los, porque um pedido pode ser, na verdade, sobre um destes e não sobre este repositório.

**Projeto-mãe**
- **[HYDRA-UMC-SERVER](https://github.com/JuanenRac/HYDRA-UMC-SERVER)** — o verdadeiro backend sem interface (REST/WebSocket) com que falam todos os clientes de controlo; este bridge não o chama: responde por MQTT, através do HYDRA-UMC-MQTT-BROKER, a quem comanda a célula.

**Projetos irmãos** — também falam com a API do próprio HYDRA-UMC-SERVER, cada um como cliente próprio
- **[HYDRA-UMC-STUDIO](https://github.com/JuanenRac/HYDRA-UMC-STUDIO)** — painel web de controlo com visualização 3D em tempo real de vários robôs.
- **[HYDRA-UMC-SUITE](https://github.com/JuanenRac/HYDRA-UMC-SUITE)** — centro de comando de enxame para desktop (PySide6) para vários servidores ao mesmo tempo, empacotado como executável independente.
- **[HYDRA-UMC-ANDROID-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-ANDROID-CONTROL)** — aplicação Android nativa de controlo, com início de sessão biométrico e um companheiro Wear OS emparelhado.
- **[HYDRA-UMC-IOS-CONTROL](https://github.com/JuanenRac/HYDRA-UMC-IOS-CONTROL)** — aplicação de controlo para iOS/iPadOS (Flutter) com sincronização WebSocket em tempo real.
- **[HYDRA-UMC-DSI](https://github.com/JuanenRac/HYDRA-UMC-DSI)** — interface tátil nativa para o ecrã tátil DSI de 7" integrado, executada no próprio CM5.
- **[HYDRA-UMC-BRIDGE-AMR](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-AMR)** — fronteira de coordenação para frotas AGV/AMR através de um publicador MQTT VDA 5050 (testado apenas contra uma frota emulada).
- **[HYDRA-UMC-BRIDGE-DROIDS](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-DROIDS)** — fronteira de coordenação para droides com patas/humanoides, com um emissor de comandos para o Boston Dynamics Spot (testado apenas contra um emulador).
- **[HYDRA-UMC-BRIDGE-LASER](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-LASER)** — coordenador de células laser que lê 3 sinais GPIO (chave, caixa e interlock) (testado apenas contra um chip simulado; não pode armar nem disparar um laser).
- **[HYDRA-UMC-BRIDGE-OPENPNP](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-OPENPNP)** — coordenador seguro de fluxo de placa de alto nível para pick-and-place com OpenPnP.
- **[HYDRA-UMC-BRIDGE-PRINTER3D](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-PRINTER3D)** — fronteira de coordenação segura para impressoras 3D Moonraker/Klipper, com comandos de trabalho reais protegidos por portas.
- **[HYDRA-UMC-BRIDGE-ROS2](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-ROS2)** — coordenador com um transporte ROS 2 baseado em rclpy importado sob demanda (ainda não executado contra uma instalação ROS 2).
- **[HYDRA-UMC-BRIDGE-UAV](https://github.com/JuanenRac/HYDRA-UMC-BRIDGE-UAV)** — fronteira de coordenação para UAV com câmara, com um emissor de comandos MAVLink (testado apenas contra um emulador).

**Diretamente relacionados**
- **[HYDRA-UMC-SDK](https://github.com/JuanenRac/HYDRA-UMC-SDK)** — o contrato JSON-Schema partilhado e a fronteira de software de controlo de trabalhos contra a qual cada bridge valida os seus comandos (não é uma função de segurança certificada).
- **[HYDRA-UMC-CONNECTOR-HUB](https://github.com/JuanenRac/HYDRA-UMC-CONNECTOR-HUB)** — registo declarativo de manifestos de adaptador e validador para conectores de máquinas externas; estende a ideia de contrato do SDK a máquinas externas sem substituir os projetos de gateway industrial.
- **[HYDRA-UMC-MQTT-BROKER](https://github.com/JuanenRac/HYDRA-UMC-MQTT-BROKER)** — o transporte real de `mqtt_transport.py` para os tópicos `hydra/bridges/cnc/...` deste bridge: estado mais bytes de controlo de jog/reinício/retoma, junto com a porta de trabalhos partilhada; ver o `docs/BRIDGE_TOPICS.md` desse repositório.
- **[HYDRA-UMC-HIL-BRIDGE](https://github.com/JuanenRac/HYDRA-UMC-HIL-BRIDGE)** — futuro caminho de evidência hardware-in-the-loop para a integração real com um controlador.

**Também fazem parte do ecossistema**

*Hardware e plataforma centrais*
- **[HYDRA-UMC](https://github.com/JuanenRac/HYDRA-UMC)** — o desenho da placa-mãe do braço robótico: anfitrião CM5 + STM32H745 de dois núcleos, pensado para orquestrar até 8 braços de ferramenta por CAN-OTA/SPI-OTA (firmware numa fase inicial; ainda sem placa montada).
- **[HYDRA-UMC-OS](https://github.com/JuanenRac/HYDRA-UMC-OS)** — camada de produto reproduzível do Raspberry Pi OS para o CM5: agente só de leitura, configuração/perfis validados, aprovisionamento Wi-Fi de primeiro contacto.

*Backend central e clientes*
- **[HYDRA-UMC-EDITOR-URDF](https://github.com/JuanenRac/HYDRA-UMC-EDITOR-URDF)** — criador/editor gráfico de URDF para desktop que envia os modelos concluídos para o catálogo do STUDIO.
- **[HYDRA-UMC-EDITOR-STL](https://github.com/JuanenRac/HYDRA-UMC-EDITOR-STL)** — editor de modelos STL para desktop que transforma/substitui/remove/adiciona peças reais no mesmo catálogo de modelos que o HYDRA-UMC-EDITOR-URDF também edita.

*Plataforma de ferramentas URTC*
- **[URTC](https://github.com/JuanenRac/URTC)** — firmware da placa física Universal Robot Tool Controller, mais de 25 perfis de ferramenta por barramento CAN.
- **[URTC-FLASHER](https://github.com/JuanenRac/URTC-FLASHER)** — ferramenta gráfica de gravação para desktop das placas URTC, por CAN-OTA e também SWD/JTAG de chip completo.
- **[URTC-TESTER](https://github.com/JuanenRac/URTC-TESTER)** — ferramenta de diagnóstico CAN em tempo real para desktop das placas URTC, com um painel por perfil de ferramenta.
- **[URTC-WEB-STUDIO](https://github.com/JuanenRac/URTC-WEB-STUDIO)** — alternativa no navegador ao URTC-TESTER através da Web Serial API, sem instalação local.
- **[URTC-GATEWAY](https://github.com/JuanenRac/URTC-GATEWAY)** — gateway à frente do barramento CAN do URTC: lista de identificadores permitidos, limite de taxa e chave de sessão; software testado num computador, ainda não num barramento real.

*Nó de IA de visão (Hailo-8)*
- **[HYDRA-UMC-VISION-NODE](https://github.com/JuanenRac/HYDRA-UMC-VISION-NODE)** — centro de integração do pipeline de visão Hailo-8, com uma verificação real de prontidão de hardware por fase.
- **[HYDRA-UMC-DETECTION-HEF](https://github.com/JuanenRac/HYDRA-UMC-DETECTION-HEF)** — registo real de modelos compilados com verificação segura de carga por arquitetura Hailo e soma de verificação.
- **[HYDRA-UMC-VISION-STREAMER](https://github.com/JuanenRac/HYDRA-UMC-VISION-STREAMER)** — pipeline GStreamer real e gerador de configuração MediaMTX com uma fronteira de integração HailoRT real.
- **[HYDRA-UMC-VISUAL-SERVOING-API](https://github.com/JuanenRac/HYDRA-UMC-VISUAL-SERVOING-API)** — lei de correção de servovisão baseada em posição com autorização condicionada ao estado da zona; ainda sem estimativa de pose por câmara.
- **[HYDRA-UMC-SAFETY-ZONES](https://github.com/JuanenRac/HYDRA-UMC-SAFETY-ZONES)** — lógica v0 de violação de zonas, atualidade da calibração e pedido de E-STOP que lê as posições dos objetos de um ficheiro; nenhum detetor a alimenta e ainda não há E-STOP físico ligado.

*Nó de IA cognitiva (Hailo-10)*
- **[HYDRA-UMC-COGNITIVE-NODE](https://github.com/JuanenRac/HYDRA-UMC-COGNITIVE-NODE)** — centro de integração do pipeline cognitivo do Hailo-10 (orquestração de LLM/VLA/voz).
- **[HYDRA-UMC-VLA-ENGINE](https://github.com/JuanenRac/HYDRA-UMC-VLA-ENGINE)** — codificação/descodificação real de tokens de ação e geração de trajetórias para um modelo Visão-Linguagem-Ação.
- **[HYDRA-UMC-VOICE-UI](https://github.com/JuanenRac/HYDRA-UMC-VOICE-UI)** — front-end de voz real (VAD + analisador de intenções) com um relé para o relógio limitado e protegido por confirmação.
- **[HYDRA-UMC-SEMANTIC-PLANNER](https://github.com/JuanenRac/HYDRA-UMC-SEMANTIC-PLANNER)** — decomposição real de tarefas baseada em regras e recuperação semântica de erros sobre os códigos de erro do MCU.
- **[HYDRA-UMC-DOCS-QA](https://github.com/JuanenRac/HYDRA-UMC-DOCS-QA)** — pesquisa real de documentos TF-IDF só com a biblioteca padrão sobre a documentação Markdown deste ecossistema.
- **[HYDRA-UMC-LOCAL-TECHNICIAN](https://github.com/JuanenRac/HYDRA-UMC-LOCAL-TECHNICIAN)** — técnico de manutenção de IA local, protegido por políticas, para o próprio ecossistema: observa, diagnostica e propõe correções; os dois níveis de maior risco não estão implementados de propósito.

*Orquestração e enxame*
- **[HYDRA-UMC-ORCHESTRATOR](https://github.com/JuanenRac/HYDRA-UMC-ORCHESTRATOR)** — centro de integração com um contrato real de relatório de saúde gRPC/Protobuf e uma máquina de estados de missões.
- **[HYDRA-UMC-JOB-DISPATCHER](https://github.com/JuanenRac/HYDRA-UMC-JOB-DISPATCHER)** — fila real de trabalhos por prioridade com deduplicação, sobre uma API HTTP real.
- **[HYDRA-UMC-NODE-HEALING](https://github.com/JuanenRac/HYDRA-UMC-NODE-HEALING)** — vigilante real de saúde da frota por gRPC com repetição/recuo e deteção de identidade incoerente.
- **[HYDRA-UMC-PATH-PLANNER-3D](https://github.com/JuanenRac/HYDRA-UMC-PATH-PLANNER-3D)** — planeador real de trajetórias 3D baseado em RRT com validação real de colisões com obstáculos e espaço de trabalho.
- **[HYDRA-UMC-SWARM-SYNC](https://github.com/JuanenRac/HYDRA-UMC-SWARM-SYNC)** — sincronização real de estado CRDT LWW-Element-Map, testada por propriedades para a convergência de várias células.

*Gémeo digital e simulação*
- **[HYDRA-UMC-TWIN](https://github.com/JuanenRac/HYDRA-UMC-TWIN)** — centro de integração do motor de gémeo digital, com um contrato real de sincronização de compatibilidade de versões.
- **[HYDRA-UMC-PHYSICS-REPLICA](https://github.com/JuanenRac/HYDRA-UMC-PHYSICS-REPLICA)** — cinemática direta real e validação de limites de juntas sobre um subconjunto real de URDF.
- **[HYDRA-UMC-SYNTHETIC-DATA-GEN](https://github.com/JuanenRac/HYDRA-UMC-SYNTHETIC-DATA-GEN)** — gerador procedural real de cenas 2D com exportação de anotações YOLO/COCO.

*Dados e análise*
- **[HYDRA-UMC-DATALAKE](https://github.com/JuanenRac/HYDRA-UMC-DATALAKE)** — armazém real de séries temporais sobre sqlite3 com uma API HTTP real de ingestão/consulta.
- **[HYDRA-UMC-ANOMALY-DETECTOR](https://github.com/JuanenRac/HYDRA-UMC-ANOMALY-DETECTOR)** — detetor real de anomalias por FFT e linha de base estatística, com monitorização de deriva.
- **[HYDRA-UMC-PRODUCTION-REPORTS](https://github.com/JuanenRac/HYDRA-UMC-PRODUCTION-REPORTS)** — cálculo real de OEE/disponibilidade sobre o histórico do DATALAKE, com exportação CSV reprodutível.
- **[HYDRA-UMC-TELEMETRY-COLLECTOR](https://github.com/JuanenRac/HYDRA-UMC-TELEMETRY-COLLECTOR)** — pipeline real de ingestão CAN/WebSocket para o DATALAKE, com deduplicação por sequência.

*Gateway industrial*
- **[HYDRA-UMC-GATEWAY-INDUSTRIAL](https://github.com/JuanenRac/HYDRA-UMC-GATEWAY-INDUSTRIAL)** — centro de integração que retransmite para protocolos industriais, com uma camada real de lista de comandos permitidos e contrapressão.
- **[HYDRA-UMC-OPCUA-SERVER](https://github.com/JuanenRac/HYDRA-UMC-OPCUA-SERVER)** — espaço de endereços OPC-UA real, verificado com uma sessão de cliente real do protocolo binário.
- **[HYDRA-UMC-MTCONNECT-ADAPTER](https://github.com/JuanenRac/HYDRA-UMC-MTCONNECT-ADAPTER)** — endpoints XML MTConnect `/probe` e `/current` reais com saída em modo degradado.

*Ferramentas complementares e operações do ecossistema*
- **[HYDRA-UMC-DASHBOARD-AI](https://github.com/JuanenRac/HYDRA-UMC-DASHBOARD-AI)** — painéis de Resumos Inteligentes e Realce de Anomalias sobre o DATALAKE/ANOMALY-DETECTOR, com um recurso estatístico honesto.
- **[HYDRA-UMC-TOOL-CLI](https://github.com/JuanenRac/HYDRA-UMC-TOOL-CLI)** — CLI da frota com um contrato de códigos de saída real e estável, um cliente vivo genuíno da API do HYDRA-UMC-SERVER.
- **[HYDRA-UMC-WATCH](https://github.com/JuanenRac/HYDRA-UMC-WATCH)** — aplicação companheira WearOS com padrões de alerta tátil e um relé de voz para o telemóvel emparelhado (ainda não executada num relógio físico).
- **[URTC-SMART-RACK](https://github.com/JuanenRac/URTC-SMART-RACK)** — firmware de um rack de montagem de placas com descodificação real do ID da ferramenta e lógica de pré-aquecimento Smart Idle.
- **[URTC-VISION-TOOL](https://github.com/JuanenRac/URTC-VISION-TOOL)** — firmware mais um companheiro de visão em Python real para uma cabeça de ferramenta de inspeção térmica/RGB.
- **[HYDRA-UMC-UPDATER](https://github.com/JuanenRac/HYDRA-UMC-UPDATER)** — ferramenta administrativa de desktop que descobre, clona e atualiza todos os repositórios deste ecossistema.
- **[HYDRA-UMC-OS-REBUILDER](https://github.com/JuanenRac/HYDRA-UMC-OS-REBUILDER)** — ferramenta de desktop para Windows/Linux que constrói uma imagem do CM5 pronta a gravar com as versões mais recentes do ecossistema, com configuração de primeiro arranque de Wi-Fi/utilizador/SSH ao estilo do Raspberry Pi Imager.
- **[HYDRA-UMC-OPS-AGENT](https://github.com/JuanenRac/HYDRA-UMC-OPS-AGENT)** — coordenador de incidentes de manutenção: um papel de borda de poucos privilégios recolhe um inventário/estado higienizado, um papel de plano de controlo mostra-o só de leitura e pede a um fornecedor de IA que sugira um diagnóstico; nunca aplica um patch nem implanta nada.
- **[HYDRA-UMC-DEV-SERVER](https://github.com/JuanenRac/HYDRA-UMC-DEV-SERVER)** — anfitrião de desenvolvimento reproduzível (Raspberry Pi 5 / CM5) que guarda o código-fonte do ecossistema e executa tarefas limitadas de compilação/teste numa fila durável; um papel de desenvolvimento dedicado, explicitamente não um CM5 operacional.

---

## 📚 Documentação e comunidade

- **[CONTRIBUTING.md](CONTRIBUTING.md)** — pilha tecnológica e diretrizes de código para um pull request.
- **[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)** — as normas de comportamento esperadas nesta comunidade.
- **[SECURITY.md](SECURITY.md)** — como comunicar uma vulnerabilidade e as áreas de segurança em que este projeto se concentra.
- **[SUPPORT.md](SUPPORT.md)** — onde fazer perguntas e comunicar erros.
- **[LICENSE.md](LICENSE.md)** — a licença deste projeto.

## 👤 AUTOR
**JuanenRac** (Electro Hobby 3D)
📧 electrohobby3d@gmail.com
📺 [youtube.com/@electrohobby3d](https://youtube.com/@electrohobby3d)

## 📜 LICENÇA
GPL-3.0-or-later - consulte LICENSE para mais detalhes.

