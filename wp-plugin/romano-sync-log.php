<?php
/**
 * Plugin Name: Romano Sync Log
 * Description: Mostra il log dell'ultimo sync scorte Socim nel backend WooCommerce.
 * Version: 1.0
 * Author: Romano Safety
 */

defined('ABSPATH') || exit;

// Aggiunge voce menu in WooCommerce
add_action('admin_menu', function () {
    add_submenu_page(
        'woocommerce',
        'Sync Scorte Socim',
        'Sync Scorte',
        'manage_woocommerce',
        'romano-sync-log',
        'romano_sync_log_page'
    );
});

// Endpoint REST per ricevere il log dallo script Python
add_action('rest_api_init', function () {
    register_rest_route('romano/v1', '/sync-log', [
        'methods'             => 'POST',
        'callback'            => 'romano_receive_sync_log',
        'permission_callback' => 'romano_verify_sync_token',
    ]);
});

function romano_verify_sync_token(WP_REST_Request $request): bool {
    $token = defined('ROMANO_SYNC_TOKEN') ? ROMANO_SYNC_TOKEN : '';
    return $token && $request->get_header('X-Sync-Token') === $token;
}

function romano_receive_sync_log(WP_REST_Request $request): WP_REST_Response {
    $body = $request->get_json_params();
    $log  = sanitize_textarea_field($body['log'] ?? '');
    if ($log) {
        update_option('romano_last_sync_log', $log);
        update_option('romano_last_sync_time', current_time('mysql'));
    }
    return new WP_REST_Response(['ok' => true], 200);
}

function romano_sync_log_page(): void {
    $log  = get_option('romano_last_sync_log', 'Nessun sync registrato.');
    $time = get_option('romano_last_sync_time', '—');
    ?>
    <div class="wrap">
        <h1>Sync Scorte Socim &rarr; WooCommerce</h1>
        <p><strong>Ultimo sync:</strong> <?php echo esc_html($time); ?></p>
        <pre style="background:#f1f1f1;padding:16px;border-radius:4px;font-size:13px;white-space:pre-wrap;"><?php
            echo esc_html($log);
        ?></pre>
        <p>
            <a href="https://github.com" target="_blank" class="button">
                Vedi log completo su GitHub Actions
            </a>
        </p>
    </div>
    <?php
}
