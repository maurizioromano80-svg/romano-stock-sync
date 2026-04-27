<?php
/**
 * Plugin Name: Romano Sync Log
 * Description: Mostra il log dell'ultimo sync scorte Socim nel backend WooCommerce.
 * Version: 1.2
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
    // Legge token da WP option (impostabile dall'admin) o da costante wp-config.php
    $token = get_option('romano_sync_token', '');
    if ( ! $token && defined('ROMANO_SYNC_TOKEN') ) $token = ROMANO_SYNC_TOKEN;
    if ( ! $token ) return false;
    $body = $request->get_json_params();
    return isset($body['token']) && hash_equals($token, $body['token']);
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
    // Salva token se inviato dal form
    if ( isset($_POST['romano_sync_token_save']) && check_admin_referer('romano_sync_settings') ) {
        update_option('romano_sync_token', sanitize_text_field($_POST['romano_sync_token']));
        echo '<div class="notice notice-success"><p>Token salvato.</p></div>';
    }

    $log   = get_option('romano_last_sync_log', 'Nessun sync registrato.');
    $time  = get_option('romano_last_sync_time', '—');
    $token = get_option('romano_sync_token', '');
    ?>
    <div class="wrap">
        <h1>Sync Scorte Socim &rarr; WooCommerce</h1>

        <h2>Impostazioni</h2>
        <form method="post">
            <?php wp_nonce_field('romano_sync_settings'); ?>
            <table class="form-table">
                <tr>
                    <th>Token sicurezza</th>
                    <td>
                        <input type="text" name="romano_sync_token" value="<?php echo esc_attr($token); ?>" size="40" />
                        <p class="description">Deve corrispondere al secret ROMANO_SYNC_TOKEN su GitHub Actions.</p>
                    </td>
                </tr>
            </table>
            <input type="submit" name="romano_sync_token_save" class="button button-primary" value="Salva token" />
        </form>

        <h2 style="margin-top:2em;">Ultimo sync</h2>
        <p><strong>Data/ora:</strong> <?php echo esc_html($time); ?></p>
        <pre style="background:#f1f1f1;padding:16px;border-radius:4px;font-size:13px;white-space:pre-wrap;"><?php echo esc_html($log); ?></pre>
        <p>
            <a href="https://github.com/maurizioromano80-svg/romano-stock-sync/actions" target="_blank" class="button">
                Vedi log completo su GitHub Actions
            </a>
        </p>
    </div>
    <?php
}
