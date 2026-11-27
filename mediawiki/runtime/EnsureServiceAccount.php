<?php
/**
 * papaia-addon-mediawiki — the service account other add-ons sign in as, and
 * its bot password, kept equal to the add-on .env.
 *
 * The account is created without a main password, so the bot password is the
 * only way in; its groups are added on every run. Setting a main password
 * (createAndPromote) would invalidate every bot password of the account. An
 * account that already exists is taken over only when it has neither a main
 * password nor a Keycloak login, so the setting cannot turn a person or the
 * break-glass admin into a member of every tier.
 *
 * The password is read from MW_SERVICE_PASSWORD, never from argv, and never
 * printed (the core's createBotPassword.php echoes it). action=login only
 * tries a bot password of at least 32 characters from 0-9 and a-w, so any
 * other value is refused here instead of failing at every sign-in. An
 * unchanged password and grant list leave the row alone, so running sessions
 * survive a restart; a changed one is written over the old, which stops
 * working right away.
 *
 * This is the only bot password: LocalSettings.php removes
 * Special:BotPasswords, and every other row is deleted here on each run, the
 * ones of a former MW_SERVICE_LOGIN included. Without MW_SERVICE_PASSWORD
 * none is left.
 */

use MediaWiki\Maintenance\Maintenance;
use MediaWiki\MediaWikiServices;
use MediaWiki\User\BotPassword;
use MediaWiki\User\CentralId\CentralIdLookup;
use MediaWiki\User\User;
use MediaWiki\User\UserIdentity;
use MediaWiki\User\UserNameUtils;
use Wikimedia\Rdbms\IDBAccessObject;

require_once getenv( 'MW_INSTALL_PATH' ) . '/maintenance/Maintenance.php';

class EnsureServiceAccount extends Maintenance {
	public function __construct() {
		parent::__construct();
		$this->addDescription( 'Create the service account, keep its bot password equal to MW_SERVICE_PASSWORD '
			. 'and remove every other bot password' );
		$this->addOption( 'user', 'Wiki user the bot password belongs to', true, true );
		$this->addOption( 'groups', 'Comma-separated groups the account is added to', true, true );
		$this->addOption( 'appid', 'Bot password app id (the part after @ in the login name)', true, true );
		$this->addOption( 'grants', 'Comma-separated grant names', true, true );
		$this->addOption( 'admin', 'The break-glass admin, which must not be the service account', true, true );
	}

	public function execute() {
		$services = MediaWikiServices::getInstance();
		$appId = $this->getOption( 'appid' );
		$password = (string)getenv( 'MW_SERVICE_PASSWORD' );

		# Everything is checked before anything is written.
		if ( $password === '' ) {
			$this->removeBotPasswords( 0, '' );
			return;
		}
		if ( !preg_match( '/^[0-9a-w]{' . BotPassword::PASSWORD_MINLENGTH . ',}$/', $password ) ) {
			$this->fatalError( 'MW_SERVICE_PASSWORD must be at least ' . BotPassword::PASSWORD_MINLENGTH
				. ' characters from 0-9 and a-w, e.g. from "openssl rand -hex 32"; action=login ignores '
				. 'any other bot password' );
		}

		$nameUtils = $services->getUserNameUtils();
		$name = $nameUtils->getCanonical( $this->getOption( 'user' ), UserNameUtils::RIGOR_CREATABLE );
		if ( $name === false ) {
			$this->fatalError( 'MW_SERVICE_LOGIN: "' . $this->getOption( 'user' ) . '" is not a valid user name' );
		}
		if ( $name === $nameUtils->getCanonical( $this->getOption( 'admin' ) ) ) {
			$this->fatalError( 'MW_SERVICE_LOGIN must not name the admin account' );
		}
		if ( $appId === '' || strlen( $appId ) > BotPassword::APPID_MAXLENGTH ) {
			$this->fatalError( 'MW_SERVICE_LOGIN: the app id after @ needs 1 to '
				. BotPassword::APPID_MAXLENGTH . ' characters' );
		}

		$grants = array_values( array_unique( array_filter( array_map( 'trim', explode( ',', $this->getOption( 'grants' ) ) ) ) ) );
		sort( $grants );
		$invalid = array_diff( $grants, $services->getGrantsInfo()->getValidGrants() );
		if ( $invalid ) {
			$this->fatalError( 'Unknown grants: ' . implode( ', ', $invalid ) );
		}

		$user = $services->getUserIdentityLookup()->getUserIdentityByName( $name, IDBAccessObject::READ_LATEST );
		if ( $user && $user->isRegistered() ) {
			$this->assertNoOtherLogin( $user );
		} else {
			$user = User::createNew( $name );
			if ( !$user ) {
				$this->fatalError( "Could not create the user $name" );
			}
			$this->output( "created the user $name\n" );
		}
		$groupManager = $services->getUserGroupManager();
		$missing = array_diff(
			array_filter( array_map( 'trim', explode( ',', $this->getOption( 'groups' ) ) ) ),
			$groupManager->getUserGroups( $user, IDBAccessObject::READ_LATEST )
		);
		if ( $missing ) {
			$groupManager->addUserToMultipleGroups( $user, $missing );
			$this->output( "added $name to " . implode( ', ', $missing ) . "\n" );
		}

		$this->removeBotPasswords(
			$services->getCentralIdLookup()->centralIdFromLocalUser( $user, CentralIdLookup::AUDIENCE_RAW ),
			$appId
		);
		$factory = $services->getPasswordFactory();
		$current = BotPassword::newFromUser( $user, $appId, IDBAccessObject::READ_LATEST );
		if ( $current ) {
			$currentGrants = $current->getGrants();
			sort( $currentGrants );
			$stored = $services->getBotPasswordStore()->getPrimaryDatabase()->newSelectQueryBuilder()
				->select( 'bp_password' )
				->from( 'bot_passwords' )
				->where( [ 'bp_user' => $current->getUserCentralId(), 'bp_app_id' => $appId ] )
				->caller( __METHOD__ )
				->fetchField();
			if ( $currentGrants === $grants && $stored !== false
				&& $factory->newFromCiphertext( $stored )->verify( $password )
			) {
				$this->output( "bot password of $name is current\n" );
				return;
			}
			$current->delete();
		}

		$bp = BotPassword::newUnsaved( [ 'user' => $user, 'appId' => $appId, 'grants' => $grants ] );
		$status = $bp ? $bp->save( 'insert', $factory->newFromPlaintext( $password ) ) : null;
		if ( !$status || !$status->isGood() ) {
			$this->fatalError( 'Could not save the bot password of ' . $name );
		}
		$this->output( "set the bot password of $name\n" );
	}

	/**
	 * Stop unless the existing account can only be the service account: one
	 * with a main password is a person or the admin, and one with a Keycloak
	 * login belongs to a person who would keep the groups added here.
	 */
	private function assertNoOtherLogin( UserIdentity $user ): void {
		$dbw = $this->getPrimaryDB();
		$mainPassword = (string)$dbw->newSelectQueryBuilder()
			->select( 'user_password' )
			->from( 'user' )
			->where( [ 'user_id' => $user->getId() ] )
			->caller( __METHOD__ )
			->fetchField();
		$keycloak = $dbw->tableExists( 'openid_connect', __METHOD__ )
			&& $dbw->newSelectQueryBuilder()
				->select( 'oidc_user' )
				->from( 'openid_connect' )
				->where( [ 'oidc_user' => $user->getId() ] )
				->caller( __METHOD__ )
				->fetchField() !== false;
		if ( $mainPassword !== '' || $keycloak ) {
			$this->fatalError( 'MW_SERVICE_LOGIN names the existing account ' . $user->getName() . ', which has '
				. ( $keycloak ? 'a Keycloak login' : 'a password' ) . '; choose a name no one uses' );
		}
	}

	/**
	 * Delete every bot password except the one of $keepUser (central id) and
	 * $keepApp; 0 deletes all of them.
	 */
	private function removeBotPasswords( int $keepUser, string $keepApp ): void {
		$services = MediaWikiServices::getInstance();
		$rows = $services->getBotPasswordStore()->getPrimaryDatabase()->newSelectQueryBuilder()
			->select( [ 'bp_user', 'bp_app_id' ] )
			->from( 'bot_passwords' )
			->caller( __METHOD__ )
			->fetchResultSet();
		foreach ( $rows as $row ) {
			$centralId = (int)$row->bp_user;
			$app = (string)$row->bp_app_id;
			if ( $centralId === $keepUser && $app === $keepApp ) {
				continue;
			}
			$bp = BotPassword::newFromCentralId( $centralId, $app, IDBAccessObject::READ_LATEST );
			if ( $bp && $bp->delete() ) {
				$owner = $services->getCentralIdLookup()->nameFromCentralId( $centralId, CentralIdLookup::AUDIENCE_RAW )
					?? "#$centralId";
				$this->output( "removed the bot password $owner@$app\n" );
			}
		}
	}
}

$maintClass = EnsureServiceAccount::class;
require_once RUN_MAINTENANCE_IF_MAIN;
