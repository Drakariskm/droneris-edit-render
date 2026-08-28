<?php
declare(strict_types=1);

/* ============================================================================
 * DRONERIS mission.php 1.6.0
 * Proxy contract for DRONERIS CORE 1.25.6 HF1
 *
 * Key behavior:
 * - POST Generator JSON to one Apps Script Web App deployment.
 * - Keep PROCESSING sentinel while upstream work is running.
 * - Persist stage diagnostics.
 * - Preserve Core UNRESOLVED as UNRESOLVED (HTTP 200), not as transport FAILED.
 * - Never fabricate a KMZ for UNRESOLVED.
 * - Persist and serve final KMZ/JSON for real COMPLETED missions.
 * - GET returns 202 while a known mission is still processing.
 * ========================================================================== */

error_reporting(E_ALL);
ini_set('display_errors', '0');
ini_set('display_startup_errors', '0');
ini_set('log_errors', '1');

/*
 * Hosting limit is approximately 180 s.
 * Keep upstream budget below PHP's total execution budget so diagnostics can
 * still be written before the request finishes.
 */
ini_set('max_execution_time', '175');
ini_set('default_socket_timeout', '160');
set_time_limit(175);
ignore_user_abort(true);

header('Access-Control-Allow-Origin: *');
header('Access-Control-Allow-Headers: Content-Type');
header('Access-Control-Allow-Methods: GET, POST, OPTIONS');
header('Access-Control-Expose-Headers: Content-Disposition, Content-Length');

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'OPTIONS') {
    http_response_code(204);
    exit;
}

/* ============================== CONFIG ==================================== */

/*
 * IMPORTANT:
 * Paste the ACTIVE DRONERIS CORE 1.25.6 HF1 Web App /exec URL below,
 * OR define DRONERIS_APPS_SCRIPT_URL in the hosting environment.
 *
 * Do not invent/reuse another deployment unless its GET /exec reports:
 *   coreVersion = 1.25.6-hf1-acquisition-unresolved-contract
 */
$appsScriptUrl = 'https://script.google.com/macros/s/AKfycbwG37e0hYQp4yA4BPQAhOzSWX7PRGlVYHKCmgUd29nCs2m8uS6qDAOiXtl6VMGr0ksE4g/exec';

$publicEndpoint = getenv('DRONERIS_PUBLIC_ENDPOINT')
    ?: 'https://fly.droneris.tech/api/mission.php';

$previewBaseUrl = getenv('DRONERIS_PREVIEW_BASE_URL')
    ?: 'https://sim.droneris.tech/';

$reportBaseUrl = getenv('DRONERIS_REPORT_BASE_URL')
    ?: 'https://log.droneris.tech/';

$storageDir = __DIR__ . DIRECTORY_SEPARATOR . 'mission-data';
$externalHttpBudgetSeconds = 160;

/* ============================== HELPERS =================================== */

function sendJson(int $status, array $data): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');

    echo json_encode(
        $data,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_INVALID_UTF8_SUBSTITUTE
    );

    exit;
}

function sanitizeMissionId(?string $value): ?string
{
    if ($value === null) {
        return null;
    }

    $value = trim($value);
    if ($value === '') {
        return null;
    }

    $value = preg_replace('/[^A-Za-z0-9_-]/', '-', $value);
    $value = trim((string) $value, '-_');

    return $value === '' ? null : substr($value, 0, 100);
}

function createMissionId(): string
{
    try {
        $suffix = strtoupper(bin2hex(random_bytes(3)));
    } catch (Throwable $e) {
        $suffix = strtoupper(substr(md5(uniqid('', true)), 0, 6));
    }

    return 'MIS-' . gmdate('Ymd-His') . '-' . $suffix;
}

function ensureStorageDirectory(string $storageDir): void
{
    if (!is_dir($storageDir)) {
        if (!mkdir($storageDir, 0750, true) && !is_dir($storageDir)) {
            sendJson(500, [
                'success' => false,
                'status' => 'STORAGE_ERROR',
                'message' => 'Mission storage directory could not be created.'
            ]);
        }
    }

    $deny = $storageDir . DIRECTORY_SEPARATOR . '.htaccess';

    if (!is_file($deny)) {
        @file_put_contents(
            $deny,
            "Require all denied\nDeny from all\n",
            LOCK_EX
        );
    }
}

function missionPath(
    string $storageDir,
    string $missionId,
    string $extension
): string {
    return $storageDir .
        DIRECTORY_SEPARATOR .
        $missionId .
        '.' .
        $extension;
}

function writeFileAtomically(string $path, string $contents): bool
{
    $tmp = $path . '.tmp-' . uniqid('', true);

    if (file_put_contents($tmp, $contents, LOCK_EX) === false) {
        return false;
    }

    if (!rename($tmp, $path)) {
        @unlink($tmp);
        return false;
    }

    @chmod($path, 0640);
    return true;
}

function elapsedMs(float $startedAt): int
{
    return (int) round((microtime(true) - $startedAt) * 1000);
}

function safeUrl(string $url): string
{
    $parts = parse_url($url);

    if (!is_array($parts)) {
        return $url;
    }

    $safe = '';

    if (isset($parts['scheme'])) {
        $safe .= $parts['scheme'] . '://';
    }

    if (isset($parts['host'])) {
        $safe .= $parts['host'];
    }

    if (isset($parts['port'])) {
        $safe .= ':' . $parts['port'];
    }

    if (isset($parts['path'])) {
        $safe .= $parts['path'];
    }

    return $safe !== '' ? $safe : $url;
}

function appendDiagnostic(
    string $path,
    float $startedAt,
    string $stage,
    string $event,
    array $details = []
): void {
    $entry = [
        'timestamp' => gmdate('c'),
        'elapsedMs' => elapsedMs($startedAt),
        'stage' => $stage,
        'event' => $event,
        'details' => $details
    ];

    $line = json_encode(
        $entry,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_INVALID_UTF8_SUBSTITUTE
    );

    if ($line !== false) {
        @file_put_contents(
            $path,
            $line . PHP_EOL,
            FILE_APPEND | LOCK_EX
        );

        error_log('[DRONERIS mission.php 1.6.0] ' . $line);
    }
}

function publicMissionUrl(
    string $endpoint,
    string $missionId,
    string $format
): string {
    return $endpoint .
        '?mission=' . rawurlencode($missionId) .
        '&format=' . rawurlencode($format);
}

function saveManifest(
    string $storageDir,
    string $missionId,
    array $manifest
): bool {
    $json = json_encode(
        $manifest,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_INVALID_UTF8_SUBSTITUTE
    );

    if ($json === false) {
        return false;
    }

    return writeFileAtomically(
        missionPath($storageDir, $missionId, 'json'),
        $json
    );
}

function saveFailureManifest(
    string $storageDir,
    string $missionId,
    string $message,
    string $stage,
    int $elapsedMs,
    array $extra = []
): array {
    $manifest = array_merge([
        'success' => false,
        'status' => 'FAILED',
        'terminalFailure' => true,
        'outcomeClass' => 'FAILED',
        'missionId' => $missionId,
        'message' => $message,
        'failedStage' => $stage,
        'elapsedMs' => $elapsedMs,
        'createdAt' => gmdate('c')
    ], $extra);

    saveManifest($storageDir, $missionId, $manifest);

    @unlink(
        missionPath(
            $storageDir,
            $missionId,
            'processing.json'
        )
    );

    return $manifest;
}

function saveUnresolvedManifest(
    string $storageDir,
    string $missionId,
    array $result,
    int $elapsedMs,
    string $publicEndpoint,
    string $previewBaseUrl,
    string $reportBaseUrl
): array {
    $core = isset($result['core']) && is_array($result['core'])
        ? $result['core']
        : [];

    $acquisitionStatus =
        $result['acquisitionStatus']
        ?? ($core['acquisition'] ?? null);

    $roadAcquisitionStatus =
        $result['roadAcquisitionStatus']
        ?? ($core['roadAcquisition'] ?? null);

    $manifest = [
        'success' => false,
        'status' => 'UNRESOLVED',
        'terminalFailure' => false,
        'outcomeClass' =>
            $result['outcomeClass']
            ?? 'ENVIRONMENT_UNRESOLVED',
        'generationState' =>
            $result['generationState']
            ?? 'AWAITING_DETERMINISTIC_SAFETY_PROOF',
        'retryable' => isset($result['retryable'])
            ? (bool) $result['retryable']
            : true,
        'missionId' => $missionId,
        'missionType' =>
            $result['missionType']
            ?? 'REAL_ESTATE',
        'subMission' =>
            $result['subMission']
            ?? 'HOUSE',
        'coreVersion' =>
            $result['coreVersion']
            ?? null,
        'bridgeVersion' =>
            $result['bridgeVersion']
            ?? null,
        'selectedRoute' =>
            $result['selectedRoute']
            ?? null,
        'selectedCandidateId' =>
            $result['selectedCandidateId']
            ?? null,
        'planningMode' =>
            $result['planningMode']
            ?? null,
        'accessMode' =>
            $core['accessMode']
            ?? null,
        'acquisitionStatus' =>
            $acquisitionStatus,
        'roadAcquisitionStatus' =>
            $roadAcquisitionStatus,
        'message' =>
            isset($result['message'])
                ? (string) $result['message']
                : 'Environment evidence is unresolved; no executable mission was exported.',
        'environment' =>
            $result['environment']
            ?? null,
        'core' => $core ?: null,
        'warnings' =>
            $result['warnings']
            ?? [],
        'errors' =>
            $result['errors']
            ?? [],
        'kmzReady' => false,
        'kmzUrl' => null,
        'previewBridge' => null,
        'jsonUrl' =>
            publicMissionUrl(
                $publicEndpoint,
                $missionId,
                'json'
            ),
        'diagnosticsUrl' =>
            publicMissionUrl(
                $publicEndpoint,
                $missionId,
                'diagnostics'
            ),
        'previewUrl' =>
            rtrim($previewBaseUrl, '/') .
            '/?mission=' .
            rawurlencode($missionId),
        'reportUrl' =>
            rtrim($reportBaseUrl, '/') .
            '/?mission=' .
            rawurlencode($missionId),
        'elapsedMs' => $elapsedMs,
        'createdAt' => gmdate('c')
    ];

    saveManifest(
        $storageDir,
        $missionId,
        $manifest
    );

    @unlink(
        missionPath(
            $storageDir,
            $missionId,
            'processing.json'
        )
    );

    return $manifest;
}

/**
 * @return array{
 *   ok:bool,
 *   status:int,
 *   body:string,
 *   error:string,
 *   effectiveUrl:string,
 *   durationMs:int,
 *   contentType:string,
 *   timedOut:bool
 * }
 */
function httpPostJson(
    string $url,
    string $json,
    int $timeout
): array {
    $startedAt = microtime(true);

    if (!function_exists('curl_init')) {
        $context = stream_context_create([
            'http' => [
                'method' => 'POST',
                'header' =>
                    "Content-Type: application/json\r\n" .
                    "Accept: application/json\r\n" .
                    "Connection: close\r\n",
                'content' => $json,
                'timeout' => $timeout,
                'ignore_errors' => true,
                'follow_location' => 1,
                'max_redirects' => 10,
                'user_agent' =>
                    'DRONERIS-Mission-Proxy/1.6'
            ]
        ]);

        $body = @file_get_contents(
            $url,
            false,
            $context
        );

        $headers =
            isset($http_response_header) &&
            is_array($http_response_header)
                ? $http_response_header
                : [];

        $status = 0;
        $contentType = '';

        foreach ($headers as $line) {
            if (
                preg_match(
                    '/^HTTP\/\S+\s+(\d{3})/i',
                    $line,
                    $m
                ) === 1
            ) {
                $status = (int) $m[1];
            }

            if (
                stripos(
                    $line,
                    'Content-Type:'
                ) === 0
            ) {
                $contentType = trim(
                    substr(
                        $line,
                        strlen('Content-Type:')
                    )
                );
            }
        }

        if ($body === false) {
            $err = error_get_last();
            $message =
                isset($err['message'])
                    ? (string) $err['message']
                    : 'HTTP stream request failed.';

            return [
                'ok' => false,
                'status' => $status,
                'body' => '',
                'error' => $message,
                'effectiveUrl' => safeUrl($url),
                'durationMs' => elapsedMs($startedAt),
                'contentType' => $contentType,
                'timedOut' =>
                    stripos($message, 'timed out') !== false
            ];
        }

        return [
            'ok' =>
                $status >= 200 &&
                $status < 300,
            'status' => $status,
            'body' => $body,
            'error' => '',
            'effectiveUrl' => safeUrl($url),
            'durationMs' => elapsedMs($startedAt),
            'contentType' => $contentType,
            'timedOut' => false
        ];
    }

    $ch = curl_init($url);

    curl_setopt_array($ch, [
        CURLOPT_RETURNTRANSFER => true,
        CURLOPT_HEADER => false,
        CURLOPT_POST => true,
        CURLOPT_POSTFIELDS => $json,
        CURLOPT_HTTPHEADER => [
            'Content-Type: application/json',
            'Accept: application/json',
            'Expect:',
            'Connection: close'
        ],
        CURLOPT_FOLLOWLOCATION => true,
        CURLOPT_MAXREDIRS => 10,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_CONNECTTIMEOUT => min(20, $timeout),
        CURLOPT_USERAGENT =>
            'DRONERIS-Mission-Proxy/1.6',
        CURLOPT_ENCODING => ''
    ]);

    if (defined('CURL_HTTP_VERSION_1_1')) {
        curl_setopt(
            $ch,
            CURLOPT_HTTP_VERSION,
            CURL_HTTP_VERSION_1_1
        );
    }

    $body = curl_exec($ch);

    $status =
        (int) curl_getinfo(
            $ch,
            CURLINFO_HTTP_CODE
        );

    $contentType =
        (string) curl_getinfo(
            $ch,
            CURLINFO_CONTENT_TYPE
        );

    $effectiveUrl = safeUrl(
        (string) curl_getinfo(
            $ch,
            CURLINFO_EFFECTIVE_URL
        )
    );

    $errno = curl_errno($ch);
    $error =
        $body === false
            ? curl_error($ch)
            : '';

    $timedOut =
        defined('CURLE_OPERATION_TIMEDOUT') &&
        $errno === CURLE_OPERATION_TIMEDOUT;

    curl_close($ch);

    return [
        'ok' =>
            $body !== false &&
            $status >= 200 &&
            $status < 300,
        'status' => $status,
        'body' =>
            $body === false
                ? ''
                : (string) $body,
        'error' => $error,
        'effectiveUrl' => $effectiveUrl,
        'durationMs' => elapsedMs($startedAt),
        'contentType' => $contentType,
        'timedOut' => $timedOut
    ];
}

/* ============================== STORAGE =================================== */

ensureStorageDirectory($storageDir);

/* ============================== GET ======================================= */

if (($_SERVER['REQUEST_METHOD'] ?? '') === 'GET') {
    $missionId = sanitizeMissionId(
        isset($_GET['mission'])
            ? (string) $_GET['mission']
            : null
    );

    if ($missionId === null) {
        sendJson(400, [
            'success' => false,
            'status' => 'INVALID_REQUEST',
            'message' =>
                'Missing or invalid mission ID.'
        ]);
    }

    $format = strtolower(
        isset($_GET['format'])
            ? (string) $_GET['format']
            : 'json'
    );

    if ($format === 'diagnostics') {
        $path = missionPath(
            $storageDir,
            $missionId,
            'diagnostics.jsonl'
        );

        if (!is_file($path)) {
            sendJson(404, [
                'success' => false,
                'status' => 'NOT_FOUND',
                'message' =>
                    'Mission diagnostics were not found.',
                'missionId' => $missionId
            ]);
        }

        $contents = file_get_contents($path);

        if ($contents === false) {
            sendJson(500, [
                'success' => false,
                'status' => 'READ_ERROR',
                'message' =>
                    'Mission diagnostics could not be read.',
                'missionId' => $missionId
            ]);
        }

        $events = [];

        foreach (
            preg_split('/\R/', trim($contents)) ?: []
            as $line
        ) {
            if (trim($line) === '') {
                continue;
            }

            $event = json_decode($line, true);

            if (is_array($event)) {
                $events[] = $event;
            }
        }

        sendJson(200, [
            'success' => true,
            'missionId' => $missionId,
            'eventCount' => count($events),
            'events' => $events
        ]);
    }

    if ($format === 'kmz') {
        $path = missionPath(
            $storageDir,
            $missionId,
            'kmz'
        );

        if (!is_file($path)) {
            sendJson(404, [
                'success' => false,
                'status' => 'NOT_FOUND',
                'message' =>
                    'Mission KMZ was not found.',
                'missionId' => $missionId
            ]);
        }

        $size = filesize($path);

        header(
            'Content-Type: application/vnd.google-earth.kmz'
        );

        header(
            'Content-Disposition: attachment; filename="' .
            $missionId .
            '.kmz"'
        );

        if ($size !== false) {
            header(
                'Content-Length: ' .
                $size
            );
        }

        readfile($path);
        exit;
    }

    if ($format !== 'json') {
        sendJson(400, [
            'success' => false,
            'status' => 'INVALID_FORMAT',
            'message' =>
                'Supported formats: json, kmz, diagnostics.'
        ]);
    }

    $finalPath = missionPath(
        $storageDir,
        $missionId,
        'json'
    );

    if (is_file($finalPath)) {
        $contents = file_get_contents($finalPath);

        if ($contents === false) {
            sendJson(500, [
                'success' => false,
                'status' => 'READ_ERROR',
                'message' =>
                    'Mission manifest could not be read.',
                'missionId' => $missionId
            ]);
        }

        http_response_code(200);
        header(
            'Content-Type: application/json; charset=utf-8'
        );
        echo $contents;
        exit;
    }

    $processingPath = missionPath(
        $storageDir,
        $missionId,
        'processing.json'
    );

    if (is_file($processingPath)) {
        $contents = file_get_contents(
            $processingPath
        );

        $processing =
            $contents !== false
                ? json_decode(
                    $contents,
                    true
                )
                : null;

        sendJson(
            202,
            is_array($processing)
                ? $processing
                : [
                    'success' => false,
                    'status' => 'PROCESSING',
                    'missionId' => $missionId,
                    'message' =>
                        'Mission is still processing.'
                ]
        );
    }

    sendJson(404, [
        'success' => false,
        'status' => 'NOT_FOUND',
        'message' =>
            'Mission was not found.',
        'missionId' => $missionId
    ]);
}

/* ============================== POST ====================================== */

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    sendJson(405, [
        'success' => false,
        'status' => 'METHOD_NOT_ALLOWED',
        'message' =>
            'Only GET, POST and OPTIONS are supported.'
    ]);
}

$requestStartedAt = microtime(true);

$rawBody = file_get_contents('php://input');

if (
    $rawBody === false ||
    trim($rawBody) === ''
) {
    sendJson(400, [
        'success' => false,
        'status' => 'INVALID_REQUEST',
        'message' =>
            'Empty request body.'
    ]);
}

$payload = json_decode(
    $rawBody,
    true
);

if (!is_array($payload)) {
    sendJson(400, [
        'success' => false,
        'status' => 'INVALID_JSON',
        'message' =>
            'Request body must be valid JSON.'
    ]);
}

/*
 * LAND/RGZ DATA ACTIONS
 * These are data-provider calls, not flight-mission generation calls.
 * Forward them directly to the active Apps Script Web App and return its JSON.
 */
$rawAction = isset($payload['action'])
    ? strtoupper(trim((string)$payload['action']))
    : '';

$landDataActions = [
    'PARCELBYNUMBER',
    'PARCELBYNUMBERS',
    'PROVIDERSTATUS',
    'LAND_PARCEL',
    'LAND_PARCELS',
    'LAND_PROVIDER_STATUS',
    'RGZ_PARCEL',
    'RGZ_MULTI_PARCELS',
    'RGZ_STATUS'
];

if (in_array($rawAction, $landDataActions, true)) {
    $landJson = json_encode(
        $payload,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_INVALID_UTF8_SUBSTITUTE
    );

    if ($landJson === false) {
        sendJson(500, [
            'success' => false,
            'status' => 'LAND_PROXY_ENCODE_FAILED'
        ]);
    }

    $landUpstream = httpPostJson(
        $appsScriptUrl,
        $landJson,
        45
    );

    if (!$landUpstream['ok']) {
        sendJson(
            $landUpstream['status'] > 0 ? $landUpstream['status'] : 502,
            [
                'success' => false,
                'status' => 'LAND_PROVIDER_UPSTREAM_FAILED',
                'message' => $landUpstream['error'] ?: 'RGZ provider request failed.',
                'upstreamStatus' => $landUpstream['status']
            ]
        );
    }

    $decodedLand = json_decode(
        (string)$landUpstream['body'],
        true
    );

    if (!is_array($decodedLand)) {
        sendJson(502, [
            'success' => false,
            'status' => 'LAND_PROVIDER_INVALID_JSON',
            'message' => 'Apps Script did not return valid JSON.'
        ]);
    }

    sendJson(200, $decodedLand);
}

$missionId = sanitizeMissionId(
    isset($payload['clientMissionId'])
        ? (string) $payload['clientMissionId']
        : (
            isset($payload['missionId'])
                ? (string) $payload['missionId']
                : (
                    isset($payload['requestId'])
                        ? (string) $payload['requestId']
                        : null
                )
        )
) ?: createMissionId();

$payload['clientMissionId'] = $missionId;
$payload['missionId'] = $missionId;
$payload['requestId'] = $missionId;

$diagnosticPath = missionPath(
    $storageDir,
    $missionId,
    'diagnostics.jsonl'
);

$processingPath = missionPath(
    $storageDir,
    $missionId,
    'processing.json'
);

$processing = [
    'success' => false,
    'status' => 'PROCESSING',
    'missionId' => $missionId,
    'startedAt' => gmdate('c'),
    'message' => 'Mission is processing.'
];

writeFileAtomically(
    $processingPath,
    (string) json_encode(
        $processing,
        JSON_UNESCAPED_UNICODE |
        JSON_UNESCAPED_SLASHES |
        JSON_INVALID_UTF8_SUBSTITUTE
    )
);

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'request_received',
    'started',
    [
        'missionId' => $missionId,
        'bodyBytes' => strlen($rawBody),
        'proxyVersion' => '1.6.0'
    ]
);

if (
    preg_match(
        '#^https://script\.google\.com/macros/s/[^/]+/exec$#',
        $appsScriptUrl
    ) !== 1
) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Apps Script deployment URL is invalid in mission.php.',
        'configuration',
        elapsedMs($requestStartedAt),
        [
            'requiredCoreVersion' =>
                '1.25.6-hf1-acquisition-unresolved-contract'
        ]
    );

    appendDiagnostic(
        $diagnosticPath,
        $requestStartedAt,
        'configuration',
        'failed',
        [
            'appsScriptUrl' =>
                safeUrl($appsScriptUrl)
        ]
    );

    sendJson(503, $manifest);
}

$forwardJson = json_encode(
    $payload,
    JSON_UNESCAPED_UNICODE |
    JSON_UNESCAPED_SLASHES |
    JSON_INVALID_UTF8_SUBSTITUTE
);

if ($forwardJson === false) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Payload could not be encoded for Apps Script.',
        'payload_encode',
        elapsedMs($requestStartedAt)
    );

    sendJson(500, $manifest);
}

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'apps_script_post',
    'started',
    [
        'target' =>
            safeUrl($appsScriptUrl),
        'payloadBytes' =>
            strlen($forwardJson),
        'timeoutSeconds' =>
            $externalHttpBudgetSeconds
    ]
);

$upstream = httpPostJson(
    $appsScriptUrl,
    $forwardJson,
    $externalHttpBudgetSeconds
);

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'apps_script_post',
    $upstream['ok']
        ? 'completed'
        : 'failed',
    [
        'httpStatus' =>
            $upstream['status'],
        'durationMs' =>
            $upstream['durationMs'],
        'effectiveUrl' =>
            $upstream['effectiveUrl'],
        'contentType' =>
            $upstream['contentType'],
        'bodyBytes' =>
            strlen($upstream['body']),
        'timedOut' =>
            $upstream['timedOut'],
        'error' =>
            $upstream['error'],
        'bodyPreview' =>
            $upstream['ok']
                ? null
                : substr(
                    $upstream['body'],
                    0,
                    1000
                )
    ]
);

if (!$upstream['ok']) {
    $httpCode =
        $upstream['timedOut']
            ? 504
            : 502;

    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        $upstream['timedOut']
            ? 'Apps Script request timed out before a Core response was received.'
            : (
                'Apps Script request failed: HTTP ' .
                $upstream['status'] .
                (
                    $upstream['error'] !== ''
                        ? ' / ' .
                            $upstream['error']
                        : ''
                )
            ),
        'apps_script_post',
        elapsedMs($requestStartedAt),
        [
            'upstreamHttpStatus' =>
                $upstream['status'],
            'upstreamTimedOut' =>
                $upstream['timedOut'],
            'upstreamContentType' =>
                $upstream['contentType'],
            'upstreamEffectiveUrl' =>
                $upstream['effectiveUrl']
        ]
    );

    sendJson($httpCode, $manifest);
}

$result = json_decode(
    $upstream['body'],
    true
);

if (!is_array($result)) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Apps Script returned a non-JSON response.',
        'apps_script_json',
        elapsedMs($requestStartedAt),
        [
            'upstreamHttpStatus' =>
                $upstream['status'],
            'contentType' =>
                $upstream['contentType'],
            'bodyPreview' =>
                substr(
                    $upstream['body'],
                    0,
                    1000
                )
        ]
    );

    appendDiagnostic(
        $diagnosticPath,
        $requestStartedAt,
        'apps_script_json',
        'failed',
        [
            'bodyPreview' =>
                substr(
                    $upstream['body'],
                    0,
                    1000
                )
        ]
    );

    sendJson(502, $manifest);
}

$coreStatus =
    isset($result['status'])
        ? strtoupper(
            (string) $result['status']
        )
        : '';

$terminalFailure =
    isset($result['terminalFailure'])
        ? (bool) $result['terminalFailure']
        : null;

$core =
    isset($result['core']) &&
    is_array($result['core'])
        ? $result['core']
        : [];

$acquisitionStatus =
    $result['acquisitionStatus']
    ?? ($core['acquisition'] ?? null);

$roadAcquisitionStatus =
    $result['roadAcquisitionStatus']
    ?? ($core['roadAcquisition'] ?? null);

$accessMode =
    $core['accessMode']
    ?? null;

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'apps_script_json',
    'completed',
    [
        'success' =>
            !empty($result['success']),
        'status' =>
            $result['status'] ?? null,
        'terminalFailure' =>
            $terminalFailure,
        'outcomeClass' =>
            $result['outcomeClass'] ?? null,
        'retryable' =>
            $result['retryable'] ?? null,
        'coreVersion' =>
            $result['coreVersion'] ?? null,
        'bridgeVersion' =>
            $result['bridgeVersion'] ?? null,
        'selectedRoute' =>
            $result['selectedRoute'] ?? null,
        'accessMode' =>
            $accessMode,
        'acquisitionStatus' =>
            $acquisitionStatus,
        'roadAcquisitionStatus' =>
            $roadAcquisitionStatus
    ]
);

/*
 * 1.6.0 CRITICAL CONTRACT:
 * Core UNRESOLVED is not a PHP transport failure.
 *
 * We deliberately return HTTP 200 so api.js receives the structured Core
 * result and can distinguish:
 *   success=false + status=UNRESOLVED + terminalFailure=false
 * from a true backend/transport failure.
 *
 * No KMZ is written.
 */
if (
    $coreStatus === 'UNRESOLVED' &&
    $terminalFailure !== true
) {
    $manifest = saveUnresolvedManifest(
        $storageDir,
        $missionId,
        $result,
        elapsedMs($requestStartedAt),
        $publicEndpoint,
        $previewBaseUrl,
        $reportBaseUrl
    );

    appendDiagnostic(
        $diagnosticPath,
        $requestStartedAt,
        'core_result',
        'unresolved',
        [
            'terminalFailure' => false,
            'retryable' =>
                $manifest['retryable'],
            'coreVersion' =>
                $manifest['coreVersion'],
            'message' =>
                $manifest['message']
        ]
    );

    sendJson(200, $manifest);
}

if (empty($result['success'])) {
    $statusForManifest =
        $coreStatus === 'REJECTED'
            ? 'REJECTED'
            : 'FAILED';

    $outcomeClass =
        $coreStatus === 'REJECTED'
            ? 'CONFIRMED_SAFETY_REJECTED'
            : (
                $result['outcomeClass']
                ?? 'FAILED'
            );

    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        isset($result['message'])
            ? (string) $result['message']
            : 'Core did not complete the mission.',
        'core_result',
        elapsedMs($requestStartedAt),
        [
            'status' =>
                $statusForManifest,
            'terminalFailure' => true,
            'outcomeClass' =>
                $outcomeClass,
            'coreStatus' =>
                $result['status'] ?? null,
            'coreVersion' =>
                $result['coreVersion'] ?? null,
            'bridgeVersion' =>
                $result['bridgeVersion'] ?? null,
            'selectedRoute' =>
                $result['selectedRoute'] ?? null,
            'accessMode' =>
                $accessMode,
            'acquisitionStatus' =>
                $acquisitionStatus,
            'roadAcquisitionStatus' =>
                $roadAcquisitionStatus,
            'core' =>
                $core ?: null,
            'warnings' =>
                $result['warnings'] ?? [],
            'errors' =>
                $result['errors'] ?? []
        ]
    );

    sendJson(
        $coreStatus === 'REJECTED'
            ? 422
            : 500,
        $manifest
    );
}

$kmzBase64 =
    isset($result['kmzBase64'])
        ? preg_replace(
            '/\s+/',
            '',
            (string) $result['kmzBase64']
        )
        : '';

if ($kmzBase64 === '') {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Core returned success but kmzBase64 is missing.',
        'kmz_source',
        elapsedMs($requestStartedAt),
        [
            'coreStatus' =>
                $result['status'] ?? null,
            'coreVersion' =>
                $result['coreVersion'] ?? null
        ]
    );

    sendJson(502, $manifest);
}

$kmzBinary = base64_decode(
    $kmzBase64,
    true
);

if (
    $kmzBinary === false ||
    strlen($kmzBinary) < 4
) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Core returned invalid kmzBase64.',
        'kmz_decode',
        elapsedMs($requestStartedAt)
    );

    sendJson(502, $manifest);
}

/* KMZ is a ZIP archive and must start with PK. */
if (
    substr(
        $kmzBinary,
        0,
        2
    ) !== 'PK'
) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'Decoded KMZ does not have a ZIP signature.',
        'kmz_signature',
        elapsedMs($requestStartedAt)
    );

    sendJson(502, $manifest);
}

$kmzPath = missionPath(
    $storageDir,
    $missionId,
    'kmz'
);

if (
    !writeFileAtomically(
        $kmzPath,
        $kmzBinary
    )
) {
    $manifest = saveFailureManifest(
        $storageDir,
        $missionId,
        'KMZ file could not be written.',
        'kmz_file_write',
        elapsedMs($requestStartedAt)
    );

    sendJson(500, $manifest);
}

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'kmz_file_write',
    'completed',
    [
        'bytes' =>
            strlen($kmzBinary)
    ]
);

$manifest = [
    'success' => true,
    'status' => 'READY',
    'terminalFailure' => false,
    'outcomeClass' => 'COMPLETED',
    'missionId' => $missionId,
    'missionType' =>
        $result['missionType']
        ?? 'REAL_ESTATE',
    'subMission' =>
        $result['subMission']
        ?? 'HOUSE',
    'coreVersion' =>
        $result['coreVersion']
        ?? null,
    'bridgeVersion' =>
        $result['bridgeVersion']
        ?? null,
    'selectedRoute' =>
        $result['selectedRoute']
        ?? null,
    'selectedCandidateId' =>
        $result['selectedCandidateId']
        ?? null,
    'planningMode' =>
        $result['planningMode']
        ?? null,
    'accessMode' =>
        $accessMode,
    'acquisitionStatus' =>
        $acquisitionStatus,
    'roadAcquisitionStatus' =>
        $roadAcquisitionStatus,
    'fileName' =>
        $missionId . '.kmz',
    'mimeType' =>
        'application/vnd.google-earth.kmz',
    'kmzBytes' =>
        strlen($kmzBinary),
    'kmzReady' => true,
    'kmzUrl' =>
        publicMissionUrl(
            $publicEndpoint,
            $missionId,
            'kmz'
        ),
    'jsonUrl' =>
        publicMissionUrl(
            $publicEndpoint,
            $missionId,
            'json'
        ),
    'diagnosticsUrl' =>
        publicMissionUrl(
            $publicEndpoint,
            $missionId,
            'diagnostics'
        ),
    'previewUrl' =>
        rtrim(
            $previewBaseUrl,
            '/'
        ) .
        '/?mission=' .
        rawurlencode($missionId),
    'reportUrl' =>
        rtrim(
            $reportBaseUrl,
            '/'
        ) .
        '/?mission=' .
        rawurlencode($missionId),
    'previewBridge' =>
        $result['previewBridge']
        ?? null,
    'core' =>
        $core ?: null,
    'warnings' =>
        $result['warnings']
        ?? [],
    'errors' =>
        $result['errors']
        ?? [],
    'createdAt' =>
        gmdate('c'),
    'elapsedMs' =>
        elapsedMs($requestStartedAt)
];

$manifestJson = json_encode(
    $manifest,
    JSON_UNESCAPED_UNICODE |
    JSON_UNESCAPED_SLASHES |
    JSON_INVALID_UTF8_SUBSTITUTE
);

if (
    $manifestJson === false ||
    !writeFileAtomically(
        missionPath(
            $storageDir,
            $missionId,
            'json'
        ),
        $manifestJson
    )
) {
    @unlink($kmzPath);

    $failure = saveFailureManifest(
        $storageDir,
        $missionId,
        'Mission manifest could not be written.',
        'manifest_write',
        elapsedMs($requestStartedAt)
    );

    sendJson(500, $failure);
}

@unlink($processingPath);

appendDiagnostic(
    $diagnosticPath,
    $requestStartedAt,
    'manifest_write',
    'completed',
    [
        'elapsedMs' =>
            elapsedMs($requestStartedAt),
        'missionId' =>
            $missionId,
        'coreVersion' =>
            $manifest['coreVersion'],
        'selectedRoute' =>
            $manifest['selectedRoute']
    ]
);

sendJson(200, $manifest);
