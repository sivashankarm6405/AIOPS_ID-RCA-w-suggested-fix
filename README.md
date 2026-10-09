# AIOps Incident Detection, RCA with Suggested Actions

A project that helps make incident investigation easier. When a service
runs into trouble, the system detects the issue, collects useful
evidence, asks a locally running AI model to explain what may have
happened, and sends the result as notification through Telegram.

## What does this project do?

Monitoring tools can tell us when something is wrong, but an alert alone
may not explain why it happened or what to check next. This project adds
an investigation flow after the alert.

It currently covers three fault scenarios: high CPU usage, high memory
usage, and repeated application crashes. For each incident, it gathers
relevant metrics, logs, container details, and recent changes. It then
prepares a short summary for a local AI model, which identifies a likely
cause and chooses a suggested fix from an approved list. The diagnosis
is sent to Telegram so the information is easy to review.

**Important:** The system recommends fixes but does not apply them
automatically. A person remains responsible for deciding what to do
next.

## How the AIOps pipeline works

``` text
Application and Telemetry
          |
          v
Prometheus Monitoring
          |
          v
Alert Rules -> Alertmanager
          |
          v
Decision Engine
(ignores duplicate alerts and creates incidents)
          |
          v
Context Collection
(metrics, logs, container details, recent changes)
          |
          v
Context Builder
(turns the evidence into a short brief)
          |
          v
Local AI / RCA
(identifies the likely cause)
          |
          v
Approved Suggested Fix
          |
          v
Telegram Notifications
```

## Tech Stack

  -----------------------------------------------------------------------
  Tech and Tools                      How it is used
  ----------------------------------- -----------------------------------
  Python and Flask                    Run the sample application, fault
                                      scenarios, and parts of the
                                      incident-handling service.

  Prometheus                          Collect service metrics and detect
                                      conditions that need attention.

  Alertmanager                        Forward firing and resolved alerts
                                      to the Decision Engine.

  Docker and Docker SDK               Run the application and collect
                                      container logs, health information,
                                      restart details, and events.

  Python and JSON                     Process alerts and save incident
                                      information and investigation
                                      results.

  Ollama with `qwen2.5:3b`            Run the local AI model that reviews
                                      the incident brief and suggests a
                                      likely cause.

  YAML (`runbooks.yml`)               Store the approved fixes, their
                                      risk levels, and the steps
                                      associated with each fix.
  -----------------------------------------------------------------------

## Implementation Stages

### Stage 1 --- Incident Detection

The existing Flask-based AIOps Alert System was extended to cover three
faults:

-   **High CPU usage:** Detects CPU usage above the configured
    threshold.
-   **High memory usage:** Detects memory consumption reaching the
    configured limit.
-   **Container restart loop:** Detects repeated container restarts.

Prometheus monitors the service, while Alertmanager forwards both firing
and resolved alerts to the Python-based Decision Engine. The engine
filters duplicate alerts and creates a JSON incident record. Telegram
notifications are used to keep track of incident updates.

### Stage 2 --- Context Collection

The context collector gathers the information an engineer would usually
check first: recent CPU and memory metrics, uptime, restart information,
application logs, container health, Docker events, and recent changes.

Each source is handled separately, so a problem collecting one type of
evidence does not stop the rest of the investigation. The collected
information is saved in an incident-specific file such as
`INC-xxx_raw.json`.

### Stage 3 --- Context Engine

The Context Builder turns the collected evidence into a shorter,
easier-to-read incident brief. It summarizes important metrics, removes
routine log messages, groups repeated entries, and puts alerts, changes,
and restart events into a timeline.

It also records information that could not be collected, so the AI can
recognize gaps instead of making assumptions. The brief is saved as
`INC-xxx_brief.json`.

### Stage 4 --- RCA and Suggested Fixes

The RCA engine sends the incident brief to the local `qwen2.5:3b` model
through Ollama. The model returns a summary, likely cause, confidence
level, supporting evidence, and a suggested fix.

To keep recommendations controlled, the available fixes are defined in
`runbooks.yml`. The AI selects from those approved options rather than
inventing its own commands. The response is checked, and uncertain or
invalid results are directed to human review. The diagnosis is saved as
`INC-xxx_rca.json`.

### Stage 5 --- Telegram Diagnosis

After the AI finishes its analysis, the system sends a readable
diagnosis to Telegram. The message includes:

-   **Summary:** What appears to have gone wrong.
-   **Likely cause:** The suspected cause and confidence level.
-   **Evidence:** The important metrics and log details behind the
    diagnosis.
-   **Suggested fix:** The recommended action, risk level, and steps.
-   **Safety note:** Confirmation that no action has been taken
    automatically.

If the AI is unavailable or cannot provide a valid response, the system
sends a warning and recommends human investigation. If the incident
resolves before the diagnosis is ready, the message reflects that
status.

## Telegram Notification Flow

  -----------------------------------------------------------------------
  Message                             When it is sent
  ----------------------------------- -----------------------------------
  🚨 Incident Opened                  When an alert fires.

  🔎 Diagnosis                        After the AI finishes its analysis.

  ⚠️ Analysis Warning                 If the AI is unavailable or its
                                      response cannot be used.

  ✅ Resolved                         When the alert clears.
  -----------------------------------------------------------------------

## Testing and Validation

The three fault scenarios were triggered individually to check how the
system handles each incident.

-   **CPU spike:** A sustained CPU load was triggered, and the alert was
    followed through its pending, firing, and resolved states.
-   **Memory leak:** Memory usage was increased toward its configured
    limit, and the resulting alert and container behaviour were checked.
-   **Crash loop:** Repeated application crashes were triggered, and the
    restart-related alert was verified.

Testing also covered duplicate-alert handling, incident record creation,
evidence collection, context brief generation, RCA output, and Telegram
delivery. The generated briefs and diagnoses were reviewed against the
evidence collected for each fault.

## Project Structure

The outline below shows the main components and their responsibilities.
Check the repository for the exact paths and any additional files.

``` text
AIOPS_ID-RCA-w-suggested-fix/
├── main.py                    # Connects incident handling and the investigation flow
├── brain/
│   ├── collector.py           # Collects metrics, logs, and container evidence
│   ├── context_builder.py     # Turns evidence into a short incident brief
│   └── rca.py                 # Requests and validates the AI diagnosis
├── notifier.py                # Formats and sends Telegram messages
├── runbooks.yml               # Approved fixes, risk levels, and action steps
├── data/
│   └── changes.log            # Recent changes used during investigation
├── app/                       # Sample Flask service and fault scenarios
├── prometheus/                # Monitoring configuration and alert rules
├── alertmanager/              # Alert routing configuration
├── incidents/                 # Incident records and investigation outputs
└── README.md                  # Project documentation
```

## Running the Project

The project uses several services, so each one needs to be configured
before testing the complete flow.

### Prerequisites

-   Python and the dependencies used by the project
-   Docker and Docker SDK for Python
-   Prometheus and Alertmanager
-   Ollama with the `qwen2.5:3b` model available locally
-   A Telegram bot and the required chat configuration

### General setup

1.  Install the Python packages listed in the project.
2.  Configure Prometheus to collect the application's metrics and load
    the alert rules.
3.  Configure Alertmanager to forward alerts to the Decision Engine.
4.  Start Ollama and confirm that `qwen2.5:3b` is available.
5.  Configure Telegram credentials using the project's environment or
    configuration files. Do not commit tokens or other secrets to
    GitHub.
6.  Start the application and supporting services using the commands and
    configuration in the repository.
7.  Trigger each fault and check the incident files and Telegram
    messages.

**Setup note:** Exact ports, environment-variable names, and startup
commands depend on the current repository configuration. Use the files
in the repository as the source of truth.

## Files Created During an Investigation

An incident can produce the following records:

-   `INC-xxx` --- the incident record.
-   `INC-xxx_raw.json` --- the evidence collected for the incident.
-   `INC-xxx_brief.json` --- the short summary prepared for the AI.
-   `INC-xxx_rca.json` --- the diagnosis and suggested fix.

## Safety and Design

-   **Local AI:** The diagnosis is generated using a model running
    through Ollama locally.
-   **Evidence-based investigation:** The AI receives a structured brief
    containing the available metrics and logs.
-   **Approved fixes only:** Suggested actions are selected from the
    predefined runbooks.
-   **Graceful handling of gaps:** Missing evidence and unusable AI
    responses are handled explicitly.
-   **Human oversight:** The system suggests what to do but does not
    carry out the fix.
-   **Secret protection:** Sensitive values and Telegram credentials
    should not be written to logs or committed to source control.

## Outcome

This project brings together monitoring, incident detection, evidence
collection, context preparation, AI-based root cause analysis, suggested
fixes, and Telegram notifications in one workflow. It demonstrates how a
local AI model can help turn an alert into a useful incident explanation
while keeping the final decision with a human.
