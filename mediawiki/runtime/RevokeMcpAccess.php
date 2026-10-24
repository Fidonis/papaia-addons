<?php
/**
 * papaia-addon-mediawiki — end one user's MCP server connections at once.
 *
 * Disabling a user in Keycloak stops new logins, but not the MCP connections
 * the user already has: the MCP server refreshes its wiki token on its own for
 * up to 30 days, and through it the user keeps reading and editing whatever
 * the wiki account may. This script does for an administrator what the user
 * can do at Special:OAuthManageMyGrants: it deletes the user's consent to the
 * papaia-mcp consumer, with the access tokens issued under it, which also
 * invalidates the refresh tokens. The user would have to sign in again, which
 * Keycloak refuses once the account is disabled.
 *
 *   php /usr/local/share/papaia-mediawiki/RevokeMcpAccess.php --user=<WikiUser>
 */

use MediaWiki\Extension\OAuth\Backend\ConsumerAcceptance;
use MediaWiki\Extension\OAuth\Backend\Utils;
use MediaWiki\Extension\OAuth\Repository\AccessTokenRepository;
use MediaWiki\Maintenance\Maintenance;
use MediaWiki\MediaWikiServices;

require_once ( getenv( 'MW_INSTALL_PATH' ) ?: '/var/www/html' ) . '/maintenance/Maintenance.php';

class RevokeMcpAccess extends Maintenance {
	private const NAME = 'papaia-mcp';

	public function __construct() {
		parent::__construct();
		$this->addDescription( "End a user's MCP server connections (all papaia-mcp consents)" );
		$this->requireExtension( 'OAuth' );
		$this->addOption( 'user', 'Wiki user name', true, true );
	}

	public function execute() {
		$user = MediaWikiServices::getInstance()->getUserFactory()->newFromName( $this->getOption( 'user' ) );
		if ( !$user || !$user->isRegistered() ) {
			$this->fatalError( 'No such user: ' . $this->getOption( 'user' ) );
		}
		$dbw = Utils::getCentralDB( DB_PRIMARY );
		$ids = $dbw->newSelectQueryBuilder()
			->select( 'oaac_id' )
			->from( 'oauth_accepted_consumer' )
			->join( 'oauth_registered_consumer', null, 'oarc_id = oaac_consumer_id' )
			->where( [ 'oaac_user_id' => Utils::getCentralIdFromLocalUser( $user ), 'oarc_name' => self::NAME ] )
			->caller( __METHOD__ )
			->fetchFieldValues();
		$tokens = new AccessTokenRepository();
		foreach ( $ids as $id ) {
			$tokens->deleteForApprovalId( (int)$id );
			ConsumerAcceptance::newFromId( $dbw, (int)$id )->delete( $dbw );
		}
		$this->output( count( $ids ) . " MCP connection consent(s) of {$user->getName()} revoked\n" );
	}
}

$maintClass = RevokeMcpAccess::class;
require_once RUN_MAINTENANCE_IF_MAIN;
