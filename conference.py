#!/usr/bin/env python
""" conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21
"""
import datetime
import re

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
from models import SpeakerForms
from models import BooleanMessage
from models import ConflictException
from models import StringMessage

from settings import WEB_CLIENT_ID

from utils import getUserId

__author__ = 'wesc+api@google.com (Wesley Chun) amended by quinlangl@gmail.com (Greg Quinlan'  # noqa

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FS_KEY = "FEATURED_SPEAKER"
ANNOUNCEMENT_TPL = ('Last chance to attend! The following conferences '
                    'are nearly sold out: %s')
ANNOUNCEMENT_FS = ('New speaker added! Now featuring %s in %s')

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": ["Default", "Topic"],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS = {
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

CONF_MO_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    confmo=messages.StringField(1),
)

SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(2, required=True),
)

SESS_POST_REQUEST = endpoints.ResourceContainer(
    SessionForm,
    websafeConferenceKey=messages.StringField(1),
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

SESSION_DATE_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1, required=True),
    sessdate=messages.StringField(2, required=True)
)

SPEAK_CITY_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    city=messages.StringField(1, required=True)
)


# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(
    name='conference',
    version='v1',
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

#  - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm.
            Args:   conf: conference entity
                    displayName: Name of conference
            Returns:    Conference Form
        """
        # Establish empty conference form
        cf = ConferenceForm()
        # For every field in the form
        for field in cf.all_fields():
            # If the Conference entitry has the field name as a property
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    # Assign the form field to the value in the entity property
                    setattr(cf, field.name, getattr(conf, field.name))
            # Special handling for the websafeKey field
            elif field.name == "websafeKey":
                # Assign the field from the conference key
                setattr(cf, field.name, conf.key.urlsafe())
        # Assign the displayName to the form from the function arg if exists
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        # Check that all required fields are present
        cf.check_initialized()
        return cf

    def _createConferenceObject(self, request):
        """Create or update Conference object. Sends confirm email to task
            Args: Request
            Returns: ConferenceForm/request
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

        # add default values for missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month on start_date
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
        taskqueue.add(params={
            'email': user.email(),
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
        data = {field.name: getattr(
            request, field.name) for field in request.all_fields()}

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)

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

    @endpoints.method(
        ConferenceForm, ConferenceForm, path='conference', http_method='POST',
        name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)

    @endpoints.method(
        CONF_POST_REQUEST, ConferenceForm,
        path='conference/{websafeConferenceKey}', http_method='PUT',
        name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)

    @endpoints.method(
        CONF_GET_REQUEST, ConferenceForm,
        path='conference/{websafeConferenceKey}', http_method='GET',
        name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s'
                % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))

    @endpoints.method(
        message_types.VoidMessage, ConferenceForms,
        path='getConferencesCreated', http_method='POST',
        name='getConferencesCreated')
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

    @endpoints.method(
        ConferenceQueryForms, ConferenceForms, path='queryConferences',
        http_method='POST', name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(
            Profile, conf.organizerUserId)) for conf in conferences]
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

    def _copySessionToForm(self, sess):
        """Copy relevant fields from Session to SessionForm."""
        # Establish blank Session Form
        sf = SessionForm()
        # For every field in the form
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; just copy others
                if field.name in ('date', 'startTime'):
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                elif field.name == 'speakerKey':
                    setattr(sf, field.name, sess.speakerKey.urlsafe())
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == 'websafeKey':
                setattr(sf, field.name, sess.key.urlsafe())
            elif field.name == 'conferenceName':
                setattr(sf, field.name, sess.key.parent().get().name)

        sf.check_initialized()
        return sf

    def _createSessionObject(self, request):
        """Create or update Session object, returning SessionForm/request.
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
            raise endpoints.BadRequestException(
                "Session 'name' field required")

        if not request.startTime:
            raise endpoints.BadRequestException(
                "Session 'startTime' field required")

        if not request.date:
            raise endpoints.BadRequestException(
                "Session 'date' field required")

        # copy SessionForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in
                request.all_fields()}

        # Verify valid speaker, store speaker entity for later
        if request.speakerName:
            speak = Speaker.query(
                Speaker.displayName == request.speakerName).get()
            if not speak:
                raise endpoints.BadRequestException(
                    "Speaker has not been entered.")

        # Remove fields from data that are not in Session
        del data['websafeKey']
        del data['conferenceName']
        del data['speakerName']
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
        # make Session key from ID
        s_key = ndb.Key(Session, s_id, parent=c_key)
        data['key'] = s_key

        # create Session, set featured speaker, return sessionForm
        Session(**data).put()
        sess = s_key.get()
        # The speakerKey seems to want to be put in this way
        if speak:
            sess.speakerKey = speak.key
            sess.put()

            taskqueue.add(params={
                'websafeSessionKey': s_key.urlsafe()},
                url='/tasks/set_featured_speaker'
            )

        return self._copySessionToForm(sess=sess)

    @endpoints.method(
        CONF_GET_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/session', http_method='GET',
        name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Get list of sessions for a given conference."""
        # get Conference object from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)
        # Query for keys of all sessions with the Conference as ancestor
        sessions = Session.query(ancestor=c_key)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s) for s in sessions])

    @endpoints.method(
        SESS_POST_REQUEST, SessionForm,
        path='conference/{websafeConferenceKey}/session', http_method='POST',
        name='createSession')
    def createSession(self, request):
        """Create new session."""
        return self._createSessionObject(request)

    @endpoints.method(
        SESSION_TYPE_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/session/type/{typeOfSession}',  # noqa
        http_method='GET', name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Given a conference, return all sessions of a specified type
        (eg lecture, keynote, workshop)"""
        # get Conference key from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)
        # Query for all sessions with the Conference as ancestor
        sessions = Session.query(ancestor=c_key)
        sesstype = sessions.filter(
            Session.typeOfSession == request.typeOfSession)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s) for s in sesstype])

    def _sessionWishlist(self, request, wishto=True):
        """Add to user's wishlist for selected session."""
        retval = None
        # get user Profile
        prof = self._getProfileFromUser()

        # check if valid session exists given websafeSessionKey
        wssk = request.websafeSessionKey
        s_key = ndb.Key(urlsafe=wssk)
        if not s_key:
            raise endpoints.NotFoundException(
                'No session found with key: %s' % wssk)
        if s_key.kind() != "Session":
            raise ConflictException(
                "This entity is not a valid session.")

        if wishto:
            # Add to wish list
            # check if session already in wishlist
            if s_key in prof.sessionWishlistKeys:
                raise ConflictException(
                    "You have already added this session to your wishlist")

            # register user, take away one seat
            prof.sessionWishlistKeys.append(s_key)
            retval = True

        else:
            # check if session already in wishlist
            if s_key in prof.sessionWishlistKeys:
                # unregister user, add back one seat
                prof.sessionWishlistKeys.remove(s_key)
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        return BooleanMessage(data=retval)

    def _getSessionsBySpeaker(self, request):
        """Given a speaker, return all sessions"""
        # get Speaker key from request; bail if not found
        sp_key = ndb.Key(urlsafe=request.websafeSpeakerKey)
        if not sp_key:
            raise endpoints.NotFoundException(
                'No speaker found with key: %s' % request.websafeSpeakerKey)

        # Query for all sessions with the speaker
        sessions = Session.query(Session.speakerKey == sp_key).fetch()

        if not sessions:
            raise endpoints.NotFoundException(
                'No sessions found found with speaker: %s'
                % sp_key.get().displayName)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s) for s in sessions])

    def _getSessionsNotTypeBeforeHour(self, request):
        """Find sessions for the conference before the given hour (24 hour) and
        not of type specified."""
        # get Conference key from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)

        # Get a list of all types except the type specified
        nottype = Session.query(
            Session.typeOfSession != request.nottype).fetch()
        if not nottype:
            raise endpoints.NotFoundException(
                'No sessions found with type not: %s' % request.nottype)
        notlist = [r.typeOfSession for r in nottype]

        # For all types in nottype, filter sessionBefore for these types
        sessnt = Session.query(Session.typeOfSession.IN(notlist))
        # Filter sessions for those in the specified conference
        c_sessnt = sessnt.filter(Session.conferenceKey == c_key)
        # Filter query for sessions that occur before the time
        qtime = datetime.time(int(request.hour), 0, 0)
        c_sessntBefore = c_sessnt.filter(Session.startTime < qtime)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s) for s in c_sessntBefore])

    def _getSessionsInWishlist(self, request):
        """Get list of sessions that user has in wishlist."""
        # get user profile
        prof = self._getProfileFromUser()

        # Fetch session from datastore.
        sessions = ndb.get_multi(prof.sessionWishlistKeys)

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(sess) for sess in sessions])

    def _getSessionsByDate(self, request):
        """Given a date, return all sessions"""
        # get Conference key from request; bail if not found
        c_key = ndb.Key(urlsafe=request.websafeConferenceKey)
        if not c_key:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' %
                request.websafeConferenceKey)

        # Validate enetered date
        sessdate = request.sessdate
        if sessdate:
            try:
                sessdate = datetime.datetime.strptime(
                    sessdate[:10], "%Y-%m-%d").date()
            except ValueError:
                raise endpoints.BadRequestException(
                    "Not a valid date. Date must be in format %Y-%m-%d.")
        # Confirm that sessdate exists within conference dates, if exist
        conf = c_key.get()
        if conf.startDate and conf.endDate:
            if conf.startDate > sessdate or sessdate > conf.endDate:
                raise endpoints.BadRequestException(
                    "Date is not within conference dates.")

        # Query for all sessions with the date
        sessions = Session.query(ancestor=c_key)
        datesess = sessions.filter(Session.date == sessdate)
        if not datesess:
            raise endpoints.BadRequestException(
                    "There are no sessions with that date.")

        # return set of SessionForm objects per Session
        return SessionForms(
            items=[self._copySessionToForm(s) for s in datesess])

    @endpoints.method(
        SESSION_KEY_REQUEST, BooleanMessage,
        path='profile/wishlist/{websafeSessionKey}', http_method='POST',
        name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """Add session to user wish list."""
        return self._sessionWishlist(request, wishto=True)

    @endpoints.method(
        SESSION_KEY_REQUEST, BooleanMessage,
        path='profile/wishlist/{websafeSessionKey}', http_method='DELETE',
        name='deleteSessionInWishlist')
    def deleteSessionInWishlist(self, request):
        """Delete session to user wish list."""
        return self._sessionWishlist(request, wishto=False)

    @endpoints.method(
        message_types.VoidMessage, SessionForms, path='profile/wishlist',
        http_method='GET', name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """Get sessions in user wish list."""
        return self._getSessionsInWishlist(request)

    @endpoints.method(
        SPEAKER_KEY_REQUEST, SessionForms,
        path='speaker/{websafeSpeakerKey}/session',
        http_method='GET', name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Get sessions by a given speaker."""
        return self._getSessionsBySpeaker(request)

    @endpoints.method(
        SESSION_NTBH_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/session/nottype/{nottype}/before/{hour}',  # noqa
        http_method='GET', name='getSessionsNotTypeBeforeHour')
    def getSessionsNotTypeBeforeHour(self, request):
        """Find sessions for the conference before the hour (24) not of
        type specified."""
        return self._getSessionsNotTypeBeforeHour(request)

    @endpoints.method(
        SESSION_DATE_REQUEST, SessionForms,
        path='conference/{websafeConferenceKey}/session/date/{sessdate}',
        http_method='GET', name='getSessionsByDate')
    def getSessionsByDate(self, request):
        """Find sessions for the conference by date (yyyymmdd)."""
        return self._getSessionsByDate(request)

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
        """Return user Profile from datastore, creating new if non-existent.
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
                key=p_key,
                displayName=user.nickname(),
                mainEmail=user.email(),
                teeShirtSize=str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()
        # return Profile
        return profile

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

    @endpoints.method(
        message_types.VoidMessage, ProfileForm, path='profile',
        http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()

    @endpoints.method(
        ProfileMiniForm, ProfileForm, path='profile', http_method='POST',
        name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)

# - - - Speaker objects - - - - - - - - - - - - - - - - - - -

    def _copySpeakerToForm(self, speak):
        """Copy relevant fields from Speaker to SpeakerForm."""
        # Initialze form, copy existing properties, validate required fields
        spf = SpeakerForm()
        for field in spf.all_fields():
            if hasattr(speak, field.name):
                setattr(spf, field.name, getattr(speak, field.name))
            elif field.name == "websafeKey":
                setattr(spf, field.name, speak.key.urlsafe())
            elif field.name == "creatorUserId":
                setattr(spf, field.name, speak.key.parent().id())
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

        # Check for existing Speaker created by that user
        # with the requested displayName
        p_key = ndb.Key(Profile, user_id)
        exist_speak = Speaker.query(
            Speaker.displayName == request.displayName, ancestor=p_key).get()
        if exist_speak:
            raise endpoints.ConflictException(
                "That speaker name already exists.")

        # copy SpeakerForm/ProtoRPC Message into dict
        data = {field.name: getattr(
            request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['creatorUserId']

        # make Profile Key from user ID
        p_key = ndb.Key(Profile, user_id)
        # allocate new Speaker ID with User key as parent
        s_id = Speaker.allocate_ids(size=1, parent=p_key)[0]
        # make Speaker key from ID
        sp_key = ndb.Key(Speaker, s_id, parent=p_key)
        data['key'] = sp_key

        # create Speaker, send email to organizer confirming
        # creation of Speaker & return (modified) SpeakerForm
        Speaker(**data).put()
        spk = sp_key.get()

        return self._copySpeakerToForm(speak=spk)

    @staticmethod
    def _setFeaturedSpeaker(websafeSessionKey):
        """ If new speaker has more than one session, set message in cache
        """
        # Get the session key and session
        s_key = ndb.Key(urlsafe=websafeSessionKey)
        sess = s_key.get()
        # if there is no speakerKey, be done
        if not sess.speakerKey:
            return
        # Get the speaker from the key
        speak = sess.speakerKey.get()
        # Query for sessions in the conference
        sessions = Session.query(ancestor=s_key.parent())
        speaksess = sessions.filter(
            Session.speakerKey == sess.speakerKey).fetch()
        # Verify that speaker has more than one session
        # If featured, assign new, return result
        count = len(speaksess)
        if count > 1:
            announcement = ANNOUNCEMENT_FS % (
                speak.displayName, (', '.join(s.name for s in speaksess)))
            memcache.set(MEMCACHE_FS_KEY, announcement)

    @endpoints.method(
        SpeakerForm, SpeakerForm, path='speaker', http_method='POST',
        name='createSpeaker')
    def createSpeaker(self, request):
        """Create new speaker."""
        return self._createSpeakerObject(request)

    @endpoints.method(
        SPEAK_CITY_REQUEST, SpeakerForms, path='speaker/city/{city}/get',
        http_method='GET', name='getSpeakerByCity')
    def getSpeakerByCity(self, request):
        """Get speakers appearing in a given city."""
        # Get conferences in the given city
        conf = Conference.query(Conference.city == request.city).fetch()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found in city: %s' % request.city)
        # Put the conference ids for that city in an array
        c_keys = [c.key for c in conf]
        # Get sessions for each of these conferences
        sessions = Session.query(Session.conferenceKey.IN(c_keys)).fetch()
        # Get unduplicated speakers in these sessions into an array
        sp_keys = []
        for s in sessions:
            if sp_keys:
                if s.speakerKey not in sp_keys:
                    sp_keys.append(s.speakerKey)
            else:
                sp_keys.append(s.speakerKey)
        # Get Speaker entities for the speaker keys
        speak = ndb.get_multi(sp_keys)
        return SpeakerForms(
            items=[self._copySpeakerToForm(speak=s) for s in speak])

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

    @endpoints.method(
        message_types.VoidMessage, StringMessage,
        path='conference/announcement/get', http_method='GET',
        name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        return StringMessage(
            data=memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY) or "")

    @endpoints.method(
        message_types.VoidMessage, StringMessage,
        path='conference/featuredspeaker/get', http_method='GET',
        name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Return Featured Speaker from memcache."""
        return StringMessage(
            data=memcache.get(MEMCACHE_FS_KEY) or "")

# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        # get user Profile
        prof = self._getProfileFromUser()

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        c_key = ndb.Key(urlsafe=wsck)
        conf = c_key.get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if c_key in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(c_key)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if c_key in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(c_key)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)

    @endpoints.method(
        message_types.VoidMessage, ConferenceForms,
        path='conferences/attending', http_method='GET',
        name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        # get user Profile
        prof = self._getProfileFromUser()
        conferences = ndb.get_multi(prof.conferenceKeysToAttend)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in
                      conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(
                conf, names[conf.organizerUserId]) for conf in conferences])

    @endpoints.method(
        CONF_GET_REQUEST, BooleanMessage,
        path='conference/{websafeConferenceKey}', http_method='POST',
        name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)

    @endpoints.method(
        CONF_GET_REQUEST, BooleanMessage,
        path='conference/{websafeConferenceKey}', http_method='DELETE',
        name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)

    @endpoints.method(
        message_types.VoidMessage, ConferenceForms, path='filterPlayground',
        http_method='GET', name='filterPlayground')
    def filterPlayground(self, request):
        """Filter Playground"""
        # sample code from instruction
        q = Conference.query()
        # field = "city"
        # operator = "="
        # value = "London"
        # f = ndb.query.FilterNode(field, operator, value)
        # q = q.filter(f)
        q = q.filter(Conference.city == "London")
        q = q.filter(Conference.topics == "Medical Innovations")
        q = q.filter(Conference.month == 6)

        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, "") for conf in q])

# registers API
api = endpoints.api_server([ConferenceApi])
