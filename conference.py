#!/usr/bin/env python
 
"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21
"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


import datetime
 
import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.ext import ndb
from google.appengine.api import memcache
from google.appengine.api import taskqueue
 
import logging

from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import TeeShirtSize
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForm
from models import ConferenceQueryForms
from models import Session
from models import SessionForm
from models import SessionForms
from models import Speaker
from models import SpeakerForm
from models import BooleanMessage
from models import ConflictException
from models import StringMessage

from settings import WEB_CLIENT_ID
 
from utils import getUserId
 
EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')

 DEFAULTS = {
     "city": "Default City",
     "maxAttendees": 0,
     "seatsAvailable": 0,
     "topics": [ "Default", "Topic" ],
 }
 
 OPERATORS = {
             'EQ':   '=',
             'GT':   '>',
             'GTEQ': '>=',
             'LT':   '<',
             'LTEQ': '<=',
             'NE':   '!='
             }
 
 FIELDS =    {
             'CITY': 'city',
             'TOPIC': 'topics',
             'MONTH': 'month',
             'MAX_ATTENDEES': 'maxAttendees',
             }
 

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(2, required=True),
)

SESSION_TYPE_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
    typeOfSession=messages.StringField(2),
)

SESSION_KEY_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSessionKey=messages.StringField(1, required=True),
)


SPEAKER_KEY_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSpeakerKey=messages.StringField(1, required=True),
)

SESSION_NTBH_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1, required=True),
    hour=messages.StringField(2, required=True),
    nottype=messages.StringField(3, required=True)
)

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

 
@endpoints.api( name='conference',
                version='v1',
                allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
                scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""
 
#  - - - Conference objects - - - - - - - - - - - - - - - - -
 
    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
             setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf
 
 
    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request.
        """
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)
 
        if not request.name:
            raise endpoints.BadRequestException(
                "Conference 'name' field required")
 
        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(
            request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])
 
        # convert dates from strings to Date objects; set month  on start_date
         if data['startDate']:
            data['startDate'] = datetime.datetime.strptime(
                data['startDate'][:10], "%Y-%m-%d").date()
            data['month'] = data['startDate'].month
        else:
            data['month'] = 0
        if data['endDate']:
            data['endDate'] = datetime.datetime.strptime(
                data['endDate'][:10], "%Y-%m-%d").date()
 
        # set seatsAvailable to be same as maxAttendees on creation
        # both for data model & outbound Message
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
            setattr(request, "seatsAvailable", data["maxAttendees"])

        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Conference ID with Profile key as parent
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        # make Conference key from ID
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id
 
        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        return request
 
 
    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.datetime.strptime(data, "%Y-%m-%d").date()
                    if field.name == 'startDate':
                        conf.month = data.month
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'\
                     % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(
                prof, 'displayName')) for conf in confs]
        )

    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)
 
        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(
                filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q
 
 
    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None
 
        for f in filters:
            filtr = {field.name: getattr(
                f, field.name) for field in f.all_fields()}
 
            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
            raise endpoints.BadRequestException(
                "Filter contains invalid field or operator.")
 
            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation used in previous filters
                # disallow the filter if inequality was performed on a 
                # different field before
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException(
                        "Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)
 
 
    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(
                    conf, names[conf.organizerUserId]) for conf in conferences]
        )


# - - - Session objects - - - - - - - - - - - - - - - - - - -

    def _copySessionToForm(self, sess, conferenceName):
        """Copy relevant fields from Session to SessionForm."""
        sf = SessionForm()
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('date') or field.name == "startTime":
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, sess.key.urlsafe())
        if conferenceName:
            setattr(sf, 'conferenceName', conferenceName)
        sf.check_initialized()
        return sf


    def _createSessionObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request.
        """
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # check if user owns conference
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        conf = c_key.get()
        if conf.organizerUserId != user_id:
            raise endpoints.UnauthorizedException(
                'User not authorized to add sessions.')

        # confirm required fields
        if not request.name:
            raise endpoints.BadRequestException("Session 'name' field required")

        # Verify valid speaker
        v = Speaker.query(Speaker.displayName==request.speaker)
        if not v.get():
            raise endpoints.BadRequestException("Speaker has not been entered.")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in \
            request.all_fields()}
        del data['websafeKey']
        del data['conferenceName']
        del data['websafeConferenceKey']

        # convert dates from strings to Date objects; set month on start_date
        if data['date']:
            data['date'] = datetime.datetime.strptime(
                data['date'][:10], "%Y-%m-%d").date()
        if data['startTime']:
            try:
                data['startTime'] = datetime.datetime.strptime(
                    data['startTime'], "%H:%M").time()
            except ValueError:
                raise endpoints.BadRequestException(
                    "startTime must be formatted 24 hour %H:%M.")    

        # allocate new Session ID with Conference key as parent
        s_id = Session.allocate_ids(size=1, parent=c_key)[0]
        # make Awaaion key from ID
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key
        data['conferenceId'] = str(c_key.id())

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Session(**data).put()
        sess = s_key.get()
        """
        taskqueue.add(params={'email': user.email(),
            'conferenceInfo': repr(request)},
            url='/tasks/send_confirmation_email'
        )
        """
        return self._copySessionToForm(sess=sess, conferenceName=conf.name)


    @endpoints.method(CONF_GET_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/session',
            http_method='GET', name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Get list of sessions for a given conference."""
        # get Conference object from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'\
                    % request.websafeConferenceKey)
        # Query for keys of all sessions with the Conference as ancestor
        sessions = Session.query(ancestor=c_key).fetch()

        # return set of SessionForm objects per Session
        return SessionForms(items=[self._copySessionToForm(s, "")\
            for s in sessions]
            )

        
    @endpoints.method(
            SESS_POST_REQUEST, SessionForm, 
            path='/conference/{websafeConferenceKey}/session',
            http_method='POST', name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)


    @endpoints.method(SESSION_TYPE_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/session/type/{typeOfSession}',  # no qa
            http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Given a conference, return all sessions of a specified type 
        (eg lecture, keynote, workshop)"""
        # get Conference key from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % \
                    request.websafeConferenceKey)
        # Query for all sessions with the Conference as ancestor
        sessions = Session.query(ancestor=c_key)
        sesstype = sessions.filter(
            Session.typeOfSession==request.typeOfSession).fetch()

        # return set of SessionForm objects per Session
        return SessionForms(items=[self._copySessionToForm(s, "")\
            for s in sesstype]
            )


    def _sessionWishlist(self, request):
        """Add to user's wishlist for selected session."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wssk = request.websafeSessionKey
        s_key = ndb.Key(urlsafe=wssk).get()
        if not s_key:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)

        # Add to wish list
        # check if user already has session to with list otherwise add
        if wssk in prof.sessionWishlistKeys:
                raise ConflictException(
                    "You have already added this session to your wishlist")

        # register user, take away one seat
        prof.sessionWishlistKeys.append(wssk)
        retval = True

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)


    def _getSessionsBySpeaker(self, request):
        """Given a speaker, return all sessions"""
        # get Conference key from request; bail if not found
        sp_key = ndb.Key(urlsafe=request.websafeSpeakerKey)
        if not sp_key:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.websafeSpeakerKey)

        speak = sp_key.get()
        # Query for all sessions with the speaker
        sessions = Session.query(Session.speaker==speak.displayName)

        # return set of SessionForm objects per Session
        return SessionForms(items=[self._copySessionToForm(s, "")\
            for s in sessions]
            )        


    def _getSessionsNotTypeBeforeHour(self, request):
        """Given a speaker, return all sessions"""
        # get Conference key from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % \
                    request.websafeConferenceKey)

        # Get a list of all type except the type speificied
        nottype = Session.query(
            Session.typeOfSession != request.nottype).fetch()
        if not nottype:
            raise endpoints.NotFoundException(
                'No sessions found with type not: %s' % \
                    request.nottype)
        notlist = []
        for r in nottype:
            logging.info(r.typeOfSession)
            notlist.append(r.typeOfSession)

        # For all types in nottype, filter sessionBefore for these types
        sessnt = Session.query(Session.typeOfSession.IN(notlist))
        logging.info(sessnt)
        # Filter sessions for those in the specified conference
        c_sessnt = sessnt.filter(Session.conferenceId == str(c_key.id()))
        # Filter query for sessions that occur before the time 
        qtime = datetime.time(int(request.hour), 0, 0)
        c_sessntBefore = c_sessnt.filter(Session.startTime < qtime).fetch()

        # return set of SessionForm objects per Session
        return SessionForms(items=[self._copySessionToForm(s, "")\
            for s in c_sessntBefore]
            ) 


    def _getSessionsInWishlist(self, request):
        """Get list of sessions that user has in wishlist."""
        # get user profile
        prof = self._getProfileFromUser()

        # Get sessionWishlistKeys from profile to make a ndb key from 
        # websafe key 
        wssk = prof.sessionWishlistKeys
        s_keys = []
        for i in wssk:
            s_keys.append(ndb.Key(urlsafe=i))

        # Fetch session from datastore. 
        sessions = ndb.get_multi(s_keys) 

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess, "") for sess in sessions])


    @endpoints.method(SESSION_KEY_REQUEST, BooleanMessage,
            path='conference/session/{websafeSessionKey}',
            http_method='POST', name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add session to user wish list."""
        return self._sessionWishlist(request)


    @endpoints.method(message_types.VoidMessage, SessionForms,
            path='session',
            http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get sessions in user wish list."""
        return self._getSessionsInWishlist(request)


    @endpoints.method(SPEAKER_KEY_REQUEST, SessionForms,
            path='speaker/{websafeSpeakerKey}/session',
            http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Add session to user wish list."""
        return self._getSessionsBySpeaker(request)


    @endpoints.method(SESSION_NTBH_REQUEST, SessionForms,
            path='conference/{websafeConferenceKey}/session/nottype/{nottype}/before/{hour}',  # no qa
            http_method='GET', name='getSessionsNotTypeBeforeHour')
    def getSessionsNotTypeBeforeHour(self, request):
        """Find sessions for the conference before the hour (24) not of 
        type specified."""
        return self._getSessionsNotTypeBeforeHour(request)

    """ 
    Two additional query types
    """

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                   setattr(pf, field.name, getattr(
                       TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent.
        """
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
 
        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
            # put the modified profile to datastore
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)
 
 
    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Speaker objects - - - - - - - - - - - - - - - - - - -

    def _copySpeakerToForm(self, speak):
        """Copy relevant fields from Speaker to SpeakerForm."""
        spf = SpeakerForm()
        for field in spf.all_fields():
            if hasattr(speak, field.name):
                setattr(spf, field.name, getattr(speak, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, sess.key.urlsafe())
        spf.check_initialized()
        return spf


    def _createSpeakerObject(self, request):
        """Create or update Speaker object, returning SpeakerForms."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # confirm required fields
        if not request.displayName:
            raise endpoints.BadRequestException(
                "Speaker 'displayName' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(
            request, field.name) for field in request.all_fields()}

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Session ID with User key as parent
        s_id = Speaker.allocate_ids(size=1, parent=p_key)[0]
        # make Awaaion key from ID
        sp_key = ndb.Key(Speaker, s_id, parent=p_key)
        data['key'] = sp_key

        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Speaker(**data).put()
        spk = sp_key.get()

        return self._copySpeakerToForm(speak=spk)
    
        
    @endpoints.method(
            SpeakerForm, SpeakerForm, 
            path='speaker',
            http_method='POST', name='createSpeaker')
    def createSpeaker(self, request):
        """Create new session."""
        return self._createSpeakerObject(request)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = ANNOUNCEMENT_TPL % (
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
            path='conference/announcement/get',
            http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(
            data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(items=[self._copyConferenceToForm(conf, names[conf.organizerUserId])\
         for conf in conferences]
        )


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='filterPlayground',
            http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city=="London")
        q = q.filter(Conference.topics=="Medical Innovations")
        q = q.filter(Conference.month==6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q]
        )

 
# registers API
api = endpoints.api_server([ConferenceApi]) 
